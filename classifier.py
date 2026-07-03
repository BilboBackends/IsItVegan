"""Phase 3: dish extraction + vegan classification via Claude.

Takes a restaurant's scraped menu text (plus Google's editorial summary and
vegetarian flag as context) and returns a structured dish list where every
dish carries a vegan verdict, confidence, reasoning, and a verbatim evidence
excerpt from the menu — per CLAUDE.md, no verdict without evidence.

Verdict taxonomy (CLAUDE.md):
  vegan            — high confidence, ingredients clearly plant-based
  likely_vegan     — probable but not certain (sauce/preparation unknown)
  vegan_adaptable  — vegan if modified (e.g. hold the cheese)
  not_vegan        — contains or likely contains animal products
  unclear          — insufficient evidence

Uses structured outputs (output_config.format) so the response is guaranteed
to match the schema — no manual JSON repair. False positives (calling a dish
vegan when it isn't) are worse than false negatives, and the prompt says so.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from config import settings

# Accuracy matters most here (false positives are the product's worst failure
# mode), so default to Opus. Override with CLASSIFIER_MODEL for cost tests.
MODEL = os.environ.get("CLASSIFIER_MODEL", "claude-opus-4-8")

# Bound per-restaurant cost: menus longer than this are truncated.
_MAX_MENU_CHARS = 14_000

VERDICTS = ("vegan", "likely_vegan", "vegan_adaptable", "not_vegan", "unclear")

_SCHEMA = {
    "type": "object",
    "properties": {
        "dishes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {
                        "type": ["string", "null"],
                        "description": "The dish's menu description, if any.",
                    },
                    "price": {"type": ["string", "null"]},
                    "category": {
                        "type": "string",
                        "enum": ["food", "drink", "dessert"],
                        "description": "drink = any beverage (soda, juice, "
                        "coffee, beer, wine, cocktails); dessert = sweets; "
                        "food = everything else.",
                    },
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "confidence": {
                        "type": "number",
                        "description": "0 to 1. How sure you are of the verdict.",
                    },
                    "reasoning": {
                        "type": "string",
                        "description": "Short: why this verdict (ingredients, "
                        "typical preparation, what's unknown).",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "Verbatim excerpt from the menu text "
                        "that supports the verdict.",
                    },
                },
                "required": [
                    "name",
                    "description",
                    "price",
                    "category",
                    "verdict",
                    "confidence",
                    "reasoning",
                    "evidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["dishes"],
    "additionalProperties": False,
}

_SYSTEM = """You extract dishes from restaurant menu text and classify each \
dish's vegan status. You reason from ingredients, dish names, and typical \
preparation — a restaurant never needs to say "vegan" for a dish to be vegan.

Verdicts:
- vegan: clearly plant-based (e.g. "falafel: chickpeas, herbs, tahini").
- likely_vegan: probably vegan but a common hidden animal ingredient is \
plausible (unknown sauce, possible butter/ghee, fish sauce in Thai curries, \
honey, chicken stock in rice/beans, egg in noodles or bread).
- vegan_adaptable: one obvious removable animal ingredient (e.g. "hold the \
cheese/feta/yogurt"). Name the modification in reasoning.
- not_vegan: contains meat/fish/dairy/egg/honey or almost certainly does.
- unclear: not enough information to judge.

Rules:
- Telling a vegan user a dish is vegan when it isn't is the worst failure. \
When preparation is genuinely unknown, prefer likely_vegan over vegan, and \
unclear over likely_vegan. Drinks, sides, and desserts count as dishes.
- Use cuisine knowledge: naan usually has dairy; pad thai usually has fish \
sauce and egg; refried beans often have lard; pizza dough is usually vegan \
but check toppings; miso soup usually uses fish dashi.
- Only extract real dishes (things a customer can order). Skip hours, \
addresses, marketing copy.
- Categorize each item: drink (any beverage — soda, juice, tea, coffee, \
beer, wine, cocktails), dessert, or food. Users looking for vegan options \
mean food; a vegan soda is not a "vegan option".
- evidence must be a verbatim excerpt of the provided text, not paraphrase.
- Extract every dish on the menu, not a sample."""


@dataclass
class ClassifiedDish:
    name: str
    description: str | None
    price: str | None
    category: str  # food | drink | dessert
    verdict: str
    confidence: float
    reasoning: str
    evidence: str


@dataclass
class ClassificationResult:
    ok: bool
    dishes: list[ClassifiedDish] = field(default_factory=list)
    error: str | None = None
    model: str = MODEL
    stop_reason: str | None = None


def classify_menu(
    menu_text: str,
    *,
    restaurant_name: str,
    editorial_summary: str | None = None,
    serves_vegetarian: bool | None = None,
    mock: bool = False,
) -> ClassificationResult:
    """Extract + classify all dishes in menu_text. Never raises."""
    if mock:
        return ClassificationResult(
            ok=True,
            model="mock",
            dishes=[
                ClassifiedDish(
                    name="Falafel Wrap",
                    description="chickpea, tahini, lettuce, tomato",
                    price="$9",
                    category="food",
                    verdict="vegan",
                    confidence=0.9,
                    reasoning="All listed ingredients are plant-based; wrap "
                    "bread is typically vegan.",
                    evidence="Falafel Wrap - chickpea, tahini, lettuce, tomato $9",
                )
            ],
        )

    if not settings.anthropic_api_key:
        return ClassificationResult(ok=False, error="ANTHROPIC_API_KEY not set")

    context_bits = [f"Restaurant: {restaurant_name}"]
    if serves_vegetarian is True:
        context_bits.append("Google says this restaurant serves vegetarian food.")
    elif serves_vegetarian is False:
        context_bits.append(
            "Google says this restaurant does NOT serve vegetarian food."
        )
    if editorial_summary:
        context_bits.append(f"Google's summary: {editorial_summary}")

    menu = menu_text[:_MAX_MENU_CHARS]
    prompt = (
        "\n".join(context_bits)
        + "\n\nMenu text scraped from the restaurant's website:\n\n"
        + menu
    )

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            system=_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        return ClassificationResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    if resp.stop_reason == "max_tokens":
        # Truncated output can't be trusted as complete JSON; log, don't store.
        return ClassificationResult(
            ok=False,
            error="Output hit max_tokens (menu too large?)",
            stop_reason=resp.stop_reason,
        )
    if resp.stop_reason == "refusal":
        return ClassificationResult(
            ok=False, error="Model refused", stop_reason=resp.stop_reason
        )

    import json

    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return ClassificationResult(ok=False, error=f"Malformed JSON: {exc}")

    dishes: list[ClassifiedDish] = []
    for d in data.get("dishes", []):
        verdict = d.get("verdict")
        if verdict not in VERDICTS:
            continue  # schema should prevent this; belt and suspenders
        conf = d.get("confidence")
        conf = max(0.0, min(1.0, float(conf))) if isinstance(conf, (int, float)) else 0.0
        name = (d.get("name") or "").strip()
        if not name:
            continue
        category = d.get("category")
        if category not in ("food", "drink", "dessert"):
            category = "food"
        dishes.append(
            ClassifiedDish(
                name=name[:200],
                description=(d.get("description") or None),
                price=(d.get("price") or None),
                category=category,
                verdict=verdict,
                confidence=conf,
                reasoning=d.get("reasoning") or "",
                evidence=d.get("evidence") or "",
            )
        )

    if not dishes:
        return ClassificationResult(ok=False, error="No dishes extracted")
    return ClassificationResult(ok=True, dishes=dishes, stop_reason=resp.stop_reason)
