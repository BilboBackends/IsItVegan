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

from dataclasses import dataclass, field

import re

from alcohol import ALCOHOL_STATUSES, classify_alcohol
from classification_providers import PRICES as _PRICES
from classification_providers import (
    ProviderResponse,
    UNTRUSTED_PROVIDERS,
    run_provider,
)
from config import settings
from dish_identity import dish_identity_key, preferred_dish_name
from guardrails import (
    apply_guardrails,
    defining_animal_ingredient,
    hidden_batter_risk,
    unqualified_animal_word,
)

# "Vegan Burger" is the restaurant declaring the dish vegan — the strongest
# text evidence there is. Word-boundary match only; Ⓥ/"V" symbols are NOT
# matched here because many menus use them for vegetarian.
_VEGAN_NAME_RE = re.compile(r"\bvegan\b", re.IGNORECASE)

# Sonnet gives near-Opus quality on structured extraction at a fraction of
# the cost (output tokens dominate here — ~100/dish). This is the METERED
# API model; override with ANTHROPIC_CLASSIFIER_MODEL. The subscription
# transports pin their own models (see classification_providers.py).
MODEL = settings.anthropic_classifier_model

# $/MTok pricing lives in classification_providers.PRICES (imported above) so
# the transports and these estimates can't drift apart.

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
    # Dietary attributes, meal tags, and key ingredients make each extracted
    # dish somewhat larger than the original vegan-only response.
    output_tokens = 250 + (chars / 120) * 110
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
                    "calories": {
                        "type": ["string", "null"],
                        "description": "Calorie text printed for this item, "
                        "including a range when shown (for example '450 cal' "
                        "or '450-700 cal'). Null when the menu does not say.",
                    },
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
                    "dairy_status": {
                        "type": "string",
                        "enum": ["free", "contains", "unclear"],
                    },
                    "gluten_status": {
                        "type": "string",
                        "enum": ["free", "contains", "unclear"],
                    },
                    "nut_status": {
                        "type": "string",
                        "enum": ["free", "contains", "unclear"],
                        "description": "Tree nuts and peanuts.",
                    },
                    "protein_level": {
                        "type": "string",
                        "enum": ["high", "moderate", "low", "unclear"],
                    },
                    "serving_role": {
                        "type": "string",
                        "enum": ["meal", "side", "unclear"],
                        "description": "Whether this is a full meal or a "
                        "side/accompaniment/snack.",
                    },
                    "alcohol_status": {
                        "type": "string",
                        "enum": ["alcoholic", "non_alcoholic", "unclear"],
                        "description": "For drinks: alcoholic (beer, wine, "
                        "cocktails, spirits, sake, hard seltzer) vs "
                        "non_alcoholic (soda, juice, coffee, tea, smoothies, "
                        "mocktails). Non-drinks: unclear.",
                    },
                    "meal_types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["breakfast", "brunch", "lunch", "dinner", "snack"],
                        },
                    },
                    "key_ingredients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Up to 8 normalized searchable ingredients.",
                    },
                },
                "required": [
                    "name",
                    "description",
                    "price",
                    "calories",
                    "category",
                    "verdict",
                    "confidence",
                    "reasoning",
                    "evidence",
                    "dairy_status",
                    "gluten_status",
                    "nut_status",
                    "protein_level",
                    "serving_role",
                    "alcohol_status",
                    "meal_types",
                    "key_ingredients",
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
cheese/feta/yogurt"). Name the modification in reasoning. The dish must \
still BE that dish after the removal: feta on a salad or cheese on a veggie \
burger is removable, but when the animal ingredient is the dish's core or \
namesake (cheese pizza, cheese empanada, mac & cheese, quesadilla, chicken \
wings), removing it doesn't leave the dish — that is not_vegan.
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
- Batter and enriched/laminated doughs hide dairy and egg: pancakes, \
waffles, crepes, muffins, donuts, biscuits, croissants, brioche, and \
challah standardly contain milk/buttermilk, butter, or eggs even when the \
menu lists only toppings — not_vegan unless the menu explicitly offers a \
vegan version. French toast is egg-based by definition. Never call these \
likely_vegan just because no animal ingredient is printed.
- Only extract real dishes (things a customer can order). Skip hours, \
addresses, marketing copy.
- Categorize each item: drink (any beverage — soda, juice, tea, coffee, \
beer, wine, cocktails), dessert, or food. Users looking for vegan options \
mean food; a vegan soda is not a "vegan option".
- evidence must be a verbatim excerpt of the provided text, not paraphrase.
- Copy calories only when the menu explicitly prints them for that item. Keep
  the displayed number or range and unit (for example "450 cal" or
  "450-700 calories"); otherwise set calories to null. Never estimate calories.
- A dish whose NAME says "vegan" ("Vegan Burger", "Vegan Pad Thai") is the \
restaurant's own declaration: verdict vegan with confidence 0.9+ unless the \
menu text visibly contradicts it. Treat an explicit menu-printed vegan label \
on the item the same way (but a bare "V" often means vegetarian — only trust \
unambiguous vegan markings).
- Calibrate, don't hedge reflexively: a dish centered on a plant protein
  (tofu, tempeh, seitan) or made entirely of vegetables, with NO animal
  ingredient named anywhere, is vegan when its listed ingredients are
  complete and plant-based (e.g. an avocado or vegetable sushi roll: rice,
  nori, vegetables — sushi rice seasoning is plant-based), and otherwise
  likely_vegan with confidence 0.75-0.85 when preparation details are merely
  unstated. Reserve confidence below 0.7 for dishes where a SPECIFIC hidden
  risk genuinely applies in that cuisine (fish sauce in Thai curries, dashi
  in miso soup, egg noodles) — and name that risk in the reasoning rather
  than vaguely doubting every sauce.
- Also classify ingredient-level dietary attributes for future search:
  - dairy_status, gluten_status, and nut_status are free, contains, or unclear.
    Use free only when the listed ingredients and normal preparation support it;
    use unclear when sauces, breading, shared ingredients, or missing detail make
    the answer uncertain. "Free" describes apparent ingredients, never kitchen
    cross-contact or allergy safety. nut_status includes peanuts and tree nuts.
  - protein_level is high only when a substantial protein source is central to
    the serving (for example tofu, tempeh, seitan, beans, lentils, eggs, meat, or
    fish), moderate for a meaningful but smaller source, low when little protein
    is apparent, and unclear when the menu provides too little information.
  - serving_role separates real meals from accompaniments so "vegan options"
    can't be inflated by a bag of chips. meal = substantial enough to be
    someone's main (sandwich, entree, burger, pizza, large bowl/salad);
    side = accompaniment, snack, or small plate (fries, chips, side salad,
    bread, hummus cup, most starters). A large/shareable appetizer that could
    serve as a main counts as meal, and an order of sushi rolls (a whole
    maki roll, 6-8 pieces) is a meal — people order rolls AS lunch or
    dinner. Menu section headings ("Sides", "Starters") are strong evidence.
    Drinks and desserts: use unclear unless obviously side-like. When
    genuinely torn, prefer side over meal — an understated count is better
    than an inflated one.
  - meal_types contains every plausible context from breakfast, brunch, lunch,
    dinner, and snack. Use menu section headings and ordinary dish usage.
  - alcohol_status separates the bar list from soft drinks: alcoholic for
    beer, wine, cocktails, spirits, sake, hard seltzer/cider; non_alcoholic
    for soda, juice, coffee, tea, smoothies, milkshakes, mocktails/virgin
    drinks. Use unclear only for non-drinks or genuinely ambiguous items.
  - key_ingredients contains up to 8 concise lowercase ingredient names useful
    for search, such as tofu, seitan, mushroom, chickpea, or rice. Do not invent
    ingredients merely because they are typical; only include menu-supported or
    strongly inherent ingredients.
- Be terse: reasoning is one short sentence, evidence a short phrase. No \
elaboration — the verdict, a reason, done.
- Extract every dish on the menu, not a sample."""

# Delta mode: the menu changed since the last classification; only the
# changes need tokens. Output cost dominates (~100 tokens/dish), so emitting
# 5 changed dishes instead of re-emitting 150 unchanged ones is the saving.
_DELTA_INSTRUCTIONS = """
DELTA MODE — this menu was classified before. You are given the previously
classified dishes as "name | price | verdict" lines, and the CURRENT menu
text. Compare them:
- Output in `dishes` ONLY items that are NEW (not in the previous list) or
  CHANGED (different price or description on the current menu, or your
  verdict/attributes would now differ). Emit each as a complete dish object.
- Output in `removed_dish_names` the EXACT previous names of dishes that no
  longer appear on the current menu. Copy names verbatim from the previous
  list — matching is exact.
- Dishes that are unchanged must NOT be output anywhere.
- If nothing changed, return empty `dishes` and empty `removed_dish_names`.
- A renamed dish is a removal of the old name plus a new dish."""


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
    calories: str | None = None
    dairy_status: str = "unclear"
    gluten_status: str = "unclear"
    nut_status: str = "unclear"
    protein_level: str = "unclear"
    serving_role: str = "unclear"  # meal | side | unclear
    alcohol_status: str = "unclear"  # alcoholic | non_alcoholic | unclear
    meal_types: list[str] = field(default_factory=list)
    key_ingredients: list[str] = field(default_factory=list)


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
    provider: str = "anthropic"
    billing: str = "api"
    # Delta mode only: prior dish names the model says left the menu.
    removed_dish_names: list[str] = field(default_factory=list)
    mode: str = "full"  # full | delta
    # Guardrail flags raised on untrusted-provider output (guardrails.py);
    # classify.py persists these to classification_audits.
    guardrail_flags: list[dict] = field(default_factory=list)


def _deduplicate_classified_dishes(
    dishes: list[ClassifiedDish],
) -> list[ClassifiedDish]:
    """Collapse repeated featured/category copies without merging variants."""
    unique: list[ClassifiedDish] = []
    positions: dict[tuple[str, str, str, str], int] = {}
    for dish in dishes:
        identity = dish_identity_key(
            dish.name, dish.price, dish.description, dish.calories
        )
        position = positions.get(identity)
        if position is None:
            positions[identity] = len(unique)
            unique.append(dish)
            continue

        existing = unique[position]
        preferred_name = preferred_dish_name(existing.name, dish.name)
        # Keep the richer/higher-confidence classification, but independently
        # preserve the most readable spelling and all useful search tags.
        chosen = dish if (
            len(dish.description or ""), dish.confidence
        ) > (
            len(existing.description or ""), existing.confidence
        ) else existing
        chosen.name = preferred_name
        chosen.meal_types = list(
            dict.fromkeys(existing.meal_types + dish.meal_types)
        )
        chosen.key_ingredients = list(
            dict.fromkeys(existing.key_ingredients + dish.key_ingredients)
        )[:8]
        unique[position] = chosen
    return unique


def result_from_data(
    data: dict,
    *,
    provider: str,
    model: str,
    billing: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_estimate: float = 0.0,
    stop_reason: str | None = None,
    mode: str = "full",
) -> ClassificationResult:
    """Validate provider/file-exchange JSON into the shared result model.

    In delta mode an empty dish list is a legitimate answer ("nothing
    changed"), and removed_dish_names is honored.
    """
    dishes: list[ClassifiedDish] = []
    raw_dishes = data.get("dishes", []) if isinstance(data, dict) else []
    for dish in raw_dishes:
        if not isinstance(dish, dict):
            continue
        verdict = dish.get("verdict")
        if verdict not in VERDICTS:
            continue
        confidence = dish.get("confidence")
        confidence = (
            max(0.0, min(1.0, float(confidence)))
            if isinstance(confidence, (int, float))
            else 0.0
        )
        name = (dish.get("name") or "").strip()
        if not name:
            continue
        # Backstop for hedging models: a name that says "vegan" is the
        # restaurant's own declaration, so a timid likely_vegan/adaptable/
        # unclear gets upgraded — unless the description plainly names an
        # unqualified animal ingredient, or the model said not_vegan (it may
        # have seen a contradiction; trust it).
        reasoning = dish.get("reasoning") or ""
        if (
            verdict in ("likely_vegan", "vegan_adaptable", "unclear")
            and _VEGAN_NAME_RE.search(name)
            and not unqualified_animal_word(str(dish.get("description") or ""))
        ):
            verdict = "vegan"
            confidence = max(confidence, 0.85)
            reasoning = (
                (reasoning + " " if reasoning else "")
                + "Dish name itself declares it vegan."
            )
        # Backstop for hidden batter: a plain pancake/waffle/croissant is
        # standardly made with milk and eggs even when the menu only lists
        # toppings — a vegan/likely_vegan verdict on one (with no vegan
        # qualifier anywhere) becomes unclear. The vegan-name rule above
        # already exempted marked-vegan versions via the qualifier check.
        # Qualifier checks below read the MENU's words only — the model's
        # reasoning routinely says "remove X to make it plant-based", which
        # would wrongly mock-qualify the dish being screened.
        menu_words = f"{name} {dish.get('description') or ''}"
        if verdict in ("vegan", "likely_vegan"):
            batter_word = hidden_batter_risk(menu_words)
            if batter_word:
                verdict = "unclear"
                confidence = min(confidence, 0.4)
                reasoning = (
                    (reasoning + " " if reasoning else "")
                    + f"[{batter_word} batter standardly contains "
                    "milk/butter/egg; no vegan version stated]"
                )
        # vegan_adaptable requires the dish to survive the modification. An
        # animal ingredient in the NAME is definitional (Cheese Empanada,
        # Shrimp Fried Rice) — removing it doesn't leave that dish, so the
        # honest verdict is not_vegan. Feta on a Greek Salad (name clean)
        # stays adaptable; "Vegan Cheese Pizza" was already upgraded above.
        if verdict == "vegan_adaptable":
            defining = defining_animal_ingredient(name, menu_words)
            if defining:
                verdict = "not_vegan"
                confidence = max(confidence, 0.8)
                reasoning = (
                    (reasoning + " " if reasoning else "")
                    + f"[{defining} is the dish's namesake — removing it "
                    "doesn't leave this dish]"
                )
        category = dish.get("category")
        if category not in ("food", "drink", "dessert"):
            category = "food"
        # Alcohol labeling: model value validated, then the word-list
        # backstop settles drinks the model left unclear ("Coke" and
        # "tequila" are not the same kind of drink). Deterministic and
        # free — also used by the backfill for pre-attribute rows.
        alcohol_status = dish.get("alcohol_status")
        if alcohol_status not in ALCOHOL_STATUSES:
            alcohol_status = "unclear"
        if category == "drink" and alcohol_status == "unclear":
            alcohol_status = classify_alcohol(
                f"{name} {dish.get('description') or ''}"
            )
        dietary_values = {"free", "contains", "unclear"}
        protein_values = {"high", "moderate", "low", "unclear"}
        meal_values = {"breakfast", "brunch", "lunch", "dinner", "snack"}
        # Guard the types, not just the values: a hand-edited exchange file
        # with "key_ingredients": "tofu, rice" would otherwise be sliced into
        # one-character "ingredients".
        raw_meals = dish.get("meal_types")
        if not isinstance(raw_meals, list):
            raw_meals = []
        raw_ingredients = dish.get("key_ingredients")
        if not isinstance(raw_ingredients, list):
            raw_ingredients = []
        meal_types = [value for value in raw_meals if value in meal_values]
        key_ingredients = [
            str(value).strip().lower()[:80]
            for value in raw_ingredients[:8]
            if str(value).strip()
        ]
        dishes.append(
            ClassifiedDish(
                name=name[:200],
                description=(dish.get("description") or None),
                price=(dish.get("price") or None),
                calories=(str(dish.get("calories")).strip()[:50] or None)
                if dish.get("calories") is not None
                else None,
                category=category,
                verdict=verdict,
                confidence=confidence,
                reasoning=reasoning,
                evidence=dish.get("evidence") or "",
                dairy_status=(
                    dish.get("dairy_status")
                    if dish.get("dairy_status") in dietary_values
                    else "unclear"
                ),
                gluten_status=(
                    dish.get("gluten_status")
                    if dish.get("gluten_status") in dietary_values
                    else "unclear"
                ),
                nut_status=(
                    dish.get("nut_status")
                    if dish.get("nut_status") in dietary_values
                    else "unclear"
                ),
                protein_level=(
                    dish.get("protein_level")
                    if dish.get("protein_level") in protein_values
                    else "unclear"
                ),
                serving_role=(
                    dish.get("serving_role")
                    if dish.get("serving_role") in ("meal", "side", "unclear")
                    else "unclear"
                ),
                alcohol_status=alcohol_status,
                meal_types=list(dict.fromkeys(meal_types)),
                key_ingredients=list(dict.fromkeys(key_ingredients)),
            )
        )
    removed_names: list[str] = []
    if mode == "delta" and isinstance(data, dict):
        raw_removed = data.get("removed_dish_names")
        if isinstance(raw_removed, list):
            removed_names = [
                str(value).strip()
                for value in raw_removed
                if str(value).strip()
            ]

    dishes = _deduplicate_classified_dishes(dishes)

    if not dishes and mode != "delta":
        return ClassificationResult(
            ok=False,
            error="No valid dishes extracted",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate=cost_estimate,
            provider=provider,
            billing=billing,
        )
    return ClassificationResult(
        ok=True,
        dishes=dishes,
        model=model,
        stop_reason=stop_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate=cost_estimate,
        provider=provider,
        billing=billing,
        removed_dish_names=removed_names,
        mode=mode,
    )


# When a provider's output cap truncates a big menu (DeepSeek most often),
# the menu is re-classified in parts of roughly this many characters and the
# parts are merged. Line-boundary splits keep dishes intact.
_CHUNK_TARGET_CHARS = 12_000


def _split_menu_lines(menu: str, target: int | None = None) -> list[str]:
    target = target or _CHUNK_TARGET_CHARS
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in menu.splitlines():
        if current and size + len(line) > target:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _hit_output_cap(response) -> bool:
    if response.stop_reason in ("length", "max_tokens"):
        return True
    return "max_tokens" in (response.error or "").lower()


# A section this small that still overflows the output cap isn't a size
# problem — stop splitting and surface the failure.
_MIN_CHUNK_CHARS = 1_500


def _classify_in_chunks(
    menu: str, *, context: str, provider: str | None, system_prompt: str
) -> ProviderResponse | None:
    """Full extraction of an oversized menu, one section at a time.

    ADAPTIVE: dense menus (e.g. 170 dishes in 12k chars of Vietnamese menu)
    can overflow the output cap even for a normal-sized section, so any
    section that hits the cap is split in half and re-queued, down to
    _MIN_CHUNK_CHARS. Returns the merged ok response, the first
    unrecoverable failure (so the normal failure path reports it), or None
    when the menu has no line breaks to split on at all.
    """
    pending = _split_menu_lines(menu)
    if len(pending) < 2:
        return None
    raw_dishes: list = []
    input_tokens = output_tokens = 0
    cost = 0.0
    last: ProviderResponse | None = None
    sections_done = 0
    while pending:
        chunk = pending.pop(0)
        prompt = (
            context
            + "\n\nMenu text scraped from the restaurant's website — ONE "
            "SECTION of a larger menu (other sections are classified "
            "separately; extract every dish in THIS section):\n\n"
            + chunk
        )
        response = run_provider(
            requested=provider,
            system_prompt=system_prompt,
            user_prompt=prompt,
            schema=_SCHEMA,
        )
        if not response.ok and _hit_output_cap(response) and len(chunk) >= 2 * _MIN_CHUNK_CHARS:
            halves = _split_menu_lines(chunk, target=max(_MIN_CHUNK_CHARS, len(chunk) // 2))
            if len(halves) >= 2:
                print(
                    f"         chunk of {len(chunk)} chars still overflowed "
                    f"the output cap — splitting into {len(halves)}"
                )
                pending = halves + pending
                continue
        if not response.ok or response.data is None:
            return response
        last = response
        sections_done += 1
        raw_dishes.extend(response.data.get("dishes") or [])
        input_tokens += response.input_tokens
        output_tokens += response.output_tokens
        cost += response.cost_estimate
    print(
        f"         oversized menu classified in {sections_done} section(s), "
        f"{len(raw_dishes)} raw dishes"
    )
    return ProviderResponse(
        ok=True,
        provider=last.provider,
        model=last.model,
        billing=last.billing,
        data={"dishes": raw_dishes},
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate=cost,
    )


def _delta_schema() -> dict:
    """The full-menu schema plus removed_dish_names, dishes allowed empty."""
    item_schema = _SCHEMA["properties"]["dishes"]["items"]
    return {
        "type": "object",
        "properties": {
            "dishes": {"type": "array", "items": item_schema},
            "removed_dish_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "EXACT names from the previous dish list that "
                "no longer appear on the current menu.",
            },
        },
        "required": ["dishes", "removed_dish_names"],
        "additionalProperties": False,
    }


def classify_menu(
    menu_text: str,
    *,
    restaurant_name: str,
    editorial_summary: str | None = None,
    serves_vegetarian: bool | None = None,
    mock: bool = False,
    provider: str | None = None,
    prior_dishes: dict[str, dict] | None = None,
) -> ClassificationResult:
    """Extract + classify all dishes in menu_text. Never raises.

    prior_dishes ({name: {price, verdict}}) switches on DELTA mode: the model
    sees the previous inventory and emits only new/changed dishes plus the
    names of removed ones — output tokens dominate this task's cost, so an
    unchanged-but-for-three-dishes menu costs ~3 dishes, not ~150.
    """
    if mock:
        return ClassificationResult(
            ok=True,
            model="mock",
            provider="mock",
            billing="none",
            dishes=[
                ClassifiedDish(
                    name="Falafel Wrap",
                    description="chickpea, tahini, lettuce, tomato",
                    price="$9",
                    calories="420 cal",
                    category="food",
                    verdict="vegan",
                    confidence=0.9,
                    reasoning="All listed ingredients are plant-based; wrap "
                    "bread is typically vegan.",
                    evidence="Falafel Wrap - chickpea, tahini, lettuce, tomato $9",
                    dairy_status="free",
                    gluten_status="contains",
                    nut_status="free",
                    protein_level="moderate",
                    serving_role="meal",
                    meal_types=["lunch", "dinner"],
                    key_ingredients=["chickpea", "tahini", "lettuce", "tomato"],
                )
            ],
        )

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

    context = "\n".join(context_bits)
    menu = menu_text[:_MAX_MENU_CHARS]
    delta = bool(prior_dishes)
    prompt = (
        context
        + "\n\nMenu text scraped from the restaurant's website:\n\n"
        + menu
    )
    system_prompt = _SYSTEM
    # The cheap tier gets its learned corrections appended (learning.py) —
    # frontier providers keep the unmodified baseline prompt so spot checks
    # measure the cheap model against a stable reference.
    chain = (provider or settings.classifier_provider or "auto").lower()
    if any(name in chain for name in UNTRUSTED_PROVIDERS):
        try:
            import learning

            guidance = learning.guidance_block()
        except Exception:
            guidance = None  # guidance is an optimization, never a blocker
        if guidance:
            system_prompt = system_prompt + "\n\n" + guidance
    # Snapshot before the delta instructions are appended — the chunked
    # retry always runs as a full extraction.
    base_system = system_prompt
    schema = _SCHEMA
    if delta:
        system_prompt = system_prompt + "\n" + _DELTA_INSTRUCTIONS
        schema = _delta_schema()
        inventory = "\n".join(
            f"{name} | {info.get('price') or '-'} | {info.get('verdict') or '-'}"
            for name, info in sorted(prior_dishes.items())
        )
        prompt += (
            "\n\nPREVIOUSLY CLASSIFIED DISHES (name | price | verdict):\n"
            + inventory
        )

    try:
        response = run_provider(
            requested=provider,
            system_prompt=system_prompt,
            user_prompt=prompt,
            schema=schema,
        )
    except Exception as exc:
        return ClassificationResult(ok=False, error=f"{type(exc).__name__}: {exc}")
    if not response.ok and _hit_output_cap(response):
        # The menu overflowed the provider's output cap (DeepSeek most
        # often). Re-run as a full extraction in parts and merge; a delta
        # that overflowed had a menu-sized change set anyway.
        chunked = _classify_in_chunks(
            menu, context=context, provider=provider, system_prompt=base_system
        )
        if chunked is not None:
            response = chunked
            delta = False
    if not response.ok or response.data is None:
        return ClassificationResult(
            ok=False,
            error=response.error or "Classification provider failed",
            model=response.model,
            stop_reason=response.stop_reason,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_estimate=response.cost_estimate,
            provider=response.provider,
            billing=response.billing,
        )
    data = response.data
    in_tok = response.input_tokens
    out_tok = response.output_tokens
    cost = response.cost_estimate

    result = result_from_data(
        data,
        mode="delta" if delta else "full",
        provider=response.provider,
        model=response.model,
        billing=response.billing,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_estimate=cost,
        stop_reason=response.stop_reason,
    )
    if (
        result.ok
        and response.provider in UNTRUSTED_PROVIDERS
        and settings.deepseek_guardrails
    ):
        result.guardrail_flags = apply_guardrails(result)
    return result
