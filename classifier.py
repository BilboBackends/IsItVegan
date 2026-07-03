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

# Sonnet gives near-Opus quality on structured extraction at a fraction of
# the cost (output tokens dominate here — ~100/dish). Override with
# CLASSIFIER_MODEL: claude-opus-4-8 for max accuracy, claude-haiku-4-5 for
# cheapest (no thinking/effort support).
MODEL = os.environ.get("CLASSIFIER_MODEL", "claude-sonnet-5")

# $/MTok (input, output) for cost reporting. Approximate list prices.
_PRICES = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Bound per-restaurant cost: menus longer than this are truncated. Matches
# the scraper's combined-pages cap (scraper._MAX_COMBINED_CHARS) — multi-page
# menus routinely exceed the old 24k bound, and truncation silently dropped
# whole menu sections from classification.
_MAX_MENU_CHARS = 50_000


def estimate_cost(menu_chars: int) -> float:
    """Pre-run cost estimate ($) for classifying a menu of this size.

    Calibrated against observed runs: input is prompt overhead plus the menu
    at ~4 chars/token; output dominates at ~70 tokens per dish, with dishes
    running ~1 per 120 chars of menu text. An estimate, not a quote — the
    dashboard shows the actual cost after a run and keeps it per restaurant.
    """
    chars = min(max(menu_chars, 0), _MAX_MENU_CHARS)
    in_price, out_price = _PRICES.get(MODEL, (3.0, 15.0))
    input_tokens = 1_200 + chars / 4
    output_tokens = 250 + (chars / 120) * 70
    return round(
        (input_tokens * in_price + output_tokens * out_price) / 1_000_000, 3
    )

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
                        "description": "ONE short sentence (max ~15 words): "
                        "why this verdict. For vegan_adaptable, name the "
                        "modification.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "Short verbatim phrase from the menu "
                        "(max ~8 words) supporting the verdict. Empty string "
                        "if the dish name itself is the evidence.",
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
but check toppings; miso soup usually uses fish dashi; yakitori/izakaya tare \
glaze often contains chicken stock or bonito — grilled items with tare are \
likely_vegan at best, never vegan.
- Only extract real dishes (things a customer can order). Skip hours, \
addresses, marketing copy.
- Categorize each item: drink (any beverage — soda, juice, tea, coffee, \
beer, wine, cocktails), dessert, or food. Users looking for vegan options \
mean food; a vegan soda is not a "vegan option".
- evidence must be a verbatim excerpt of the provided text, not paraphrase.
- Be terse: reasoning is one short sentence, evidence a short phrase. No \
elaboration — the verdict, a reason, done.
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
    input_tokens: int = 0
    output_tokens: int = 0
    cost_estimate: float = 0.0  # USD, approximate list price


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
        # Google's negative flag is often missing/wrong (e.g. izakayas with
        # plenty of vegetable dishes). Don't let it bias verdicts.
        context_bits.append(
            "Google's listing doesn't flag vegetarian options, but that signal "
            "is unreliable — judge each dish purely from the menu itself."
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
        # Stream so large menus can emit a full dish list — big izakaya/BBQ
        # menus overflow a non-streaming response cap (observed live), and a
        # truncated dish list must never be stored.
        kwargs = dict(
            model=MODEL,
            max_tokens=64000,
            system=_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        if "haiku" not in MODEL:
            # Cap thinking/verbosity spend — output tokens dominate the cost
            # of this task. (Haiku doesn't support adaptive thinking/effort.)
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"]["effort"] = "medium"
        with client.messages.stream(**kwargs) as stream:
            resp = stream.get_final_message()
    except Exception as exc:
        return ClassificationResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    in_price, out_price = _PRICES.get(MODEL, (5.0, 25.0))
    cost = (in_tok * in_price + out_tok * out_price) / 1_000_000

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
        return ClassificationResult(
            ok=False,
            error="No dishes extracted",
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_estimate=cost,
        )
    return ClassificationResult(
        ok=True,
        dishes=dishes,
        stop_reason=resp.stop_reason,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_estimate=cost,
    )
