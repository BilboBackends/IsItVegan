"""Guardrails for classifications produced by untrusted (cheap) models.

The product principle is that a false positive — telling a vegan user a dish
is vegan when it isn't — is the worst failure. Frontier models earn trust;
the cheap bulk tier (DeepSeek) does not, so every one of its results passes
through these deterministic checks before it is stored:

- dish-level HARD rule: a vegan/likely_vegan verdict on a dish whose name,
  description, or ingredients contain an unambiguous animal word (with no
  mock/plant qualifier anywhere) is DOWNGRADED to unclear and flagged.
- run-level SOFT rules: an implausibly high vegan rate or a suspiciously
  uniform confidence distribution flags the whole run for audit without
  touching the verdicts.

Every flag is persisted to classification_audits (see db.record_audits) so
the Admin dashboard can monitor how often the cheap tier trips the rails —
rising flag rates mean the spot-check auditor should run.
"""
from __future__ import annotations

import re
import statistics

# Unambiguous animal ingredients. Deliberately conservative: words like
# "wing" or "burger" are omitted because plant versions are common; every
# word here names an animal product outright.
_ANIMAL_WORDS = (
    "chicken", "beef", "pork", "bacon", "ham", "sausage", "pepperoni",
    "salami", "chorizo", "prosciutto", "pastrami", "brisket", "meatball",
    "steak", "veal", "lamb", "turkey", "duck", "foie gras",
    "fish", "salmon", "tuna", "anchovy", "anchovies", "sardine", "cod",
    "mahi", "tilapia", "shrimp", "prawn", "crab", "lobster", "clam",
    "mussel", "oyster", "scallop", "squid", "calamari", "octopus", "eel",
    "unagi", "caviar", "roe",
    "cheese", "mozzarella", "parmesan", "cheddar", "feta", "ricotta",
    "provolone", "gouda", "brie", "queso", "burrata", "halloumi",
    "butter", "cream", "milk", "yogurt", "ghee", "custard", "gelato",
    "egg", "eggs", "mayo", "mayonnaise", "aioli", "ranch", "alfredo",
    "honey", "gelatin", "lard", "tallow", "whey",
)

# A nearby mock/plant qualifier means the animal word is a plant version
# ("vegan cheese", "soy chorizo", "Impossible beef") — no flag.
_MOCK_WORDS = (
    "vegan", "plant-based", "plant based", "dairy-free", "dairy free",
    "non-dairy", "meatless", "mock", "imitation", "impossible", "beyond",
    "tofu", "tempeh", "seitan", "soy", "cashew", "almond", "oat", "coconut",
    "nutritional yeast", "vegetable broth", "veggie", "jackfruit",
    "substitute", "alternative", "faux", "chick'n", "chikn", "no-egg",
    "eggless", "aquafaba", "flax",
)

_ANIMAL_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _ANIMAL_WORDS) + r")\b",
    re.IGNORECASE,
)


def _mock_qualified(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _MOCK_WORDS)


# Batter/laminated-dough dishes whose STANDARD recipe contains milk, butter,
# or eggs even though the menu never names them: a plain "Blueberry Waffles"
# is almost never vegan. French toast is egg-based by definition.
_BATTER_WORDS = (
    "pancake", "pancakes", "waffle", "waffles", "french toast", "crepe",
    "crepes", "crêpe", "crêpes", "croissant", "croissants", "brioche",
    "challah", "biscuit", "biscuits", "muffin", "muffins", "donut",
    "donuts", "doughnut", "doughnuts",
)
_BATTER_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _BATTER_WORDS) + r")\b",
    re.IGNORECASE,
)

# "Pancake"/"crepe" dishes whose TRADITIONAL batter is plant-based — Asian
# scallion pancakes are flour-water-oil dough, bánh xèo is rice flour and
# coconut milk, dosa/injera/socca are naturally vegan ferments. "chay" is
# Vietnamese for vegetarian/vegan. Western buttermilk batter rules must not
# downgrade these.
_BATTER_EXEMPT = (
    "scallion pancake", "green onion pancake", "spring onion pancake",
    "banh xeo", "bánh xèo", "dosa", "injera", "socca", "jianbing",
    "moo shu", "mu shu", "chay",
)


def hidden_batter_risk(text: str) -> str | None:
    """The batter word found when text names a standardly-non-vegan batter
    dish with no vegan/plant qualifier anywhere; None otherwise."""
    lowered = text.lower()
    if any(word in lowered for word in _BATTER_EXEMPT):
        return None
    match = _BATTER_RE.search(text)
    if match and not _mock_qualified(text):
        return match.group(0)
    return None


# Dishes whose NAME doesn't contain an animal word but which ARE dairy/egg
# by definition — a Margherita or Caprese without mozzarella isn't that dish.
_ANIMAL_DEFINED_DISHES = (
    "margherita", "caprese", "bianca", "quattro formaggi", "formaggio",
    "quesadilla", "mac and cheese", "mac & cheese", "grilled cheese",
    "saganaki", "tiramisu", "cannoli", "flan", "creme brulee",
    "crème brûlée", "milk tea",
)
_ANIMAL_DEFINED_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _ANIMAL_DEFINED_DISHES) + r")\b",
    re.IGNORECASE,
)


def defining_animal_ingredient(name: str, menu_text: str) -> str | None:
    """The animal word (or animal-defined dish name) in a dish's NAME,
    unless the MENU's own words carry a mock/plant qualifier.

    A name-level animal ingredient is DEFINITIONAL: a "Cheese Empanada"
    without cheese isn't that dish, so vegan_adaptable is wrong for it —
    unlike feta on a salad, where the name survives the removal.

    menu_text must be the menu's words only (name + description). NEVER
    include the model's reasoning: adaptation advice like "remove the
    mozzarella to make it plant-based" would mock-qualify the very dish
    the rule exists to catch (how Antonio's Cheese Pizza slipped through).
    """
    match = _ANIMAL_RE.search(name) or _ANIMAL_DEFINED_RE.search(name)
    if match and not _mock_qualified(menu_text):
        return match.group(0)
    return None


# Pizza-family dishes: baked-in cheese is not a removable topping. Product
# decision: a pizza that doesn't already offer vegan cheese is not vegan.
_PIZZA_RE = re.compile(
    r"\b(pizza|pizzas|pizzette|flatbread|flatbreads|calzone|stromboli)\b",
    re.IGNORECASE,
)
_DAIRY_CHEESE_WORDS = (
    "cheese", "mozzarella", "parmesan", "parmigiano", "pecorino",
    "grana padano", "asiago", "fontina", "gorgonzola", "ricotta",
    "provolone", "burrata", "stracciatella", "taleggio", "feta", "cheddar",
    "queso", "brie", "gouda", "halloumi", "manchego",
)
_DAIRY_CHEESE_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _DAIRY_CHEESE_WORDS) + r")\b",
    re.IGNORECASE,
)


def baked_in_dairy_cheese(name: str, menu_text: str) -> str | None:
    """The dairy cheese named on a pizza/flatbread with no vegan qualifier.

    Cheese on pizza is baked into every slice, not picked off — so unless
    the menu itself offers a vegan cheese, the pizza is not vegan and
    'adaptable' is the wrong promise.
    """
    if not _PIZZA_RE.search(name):
        return None
    match = _DAIRY_CHEESE_RE.search(menu_text)
    if match and not _mock_qualified(menu_text):
        return match.group(0)
    return None


def unqualified_animal_word(text: str) -> bool:
    """True when text plainly names an animal ingredient with no mock/plant
    qualifier anywhere — the shared "is this actually risky?" primitive."""
    match = _ANIMAL_RE.search(text)
    return bool(match) and not _mock_qualified(text)


def _dish_text(dish) -> str:
    return " ".join(
        str(part)
        for part in (
            dish.name,
            dish.description or "",
            dish.reasoning or "",
            dish.evidence or "",
            " ".join(dish.key_ingredients or []),
        )
    )


def apply_guardrails(result) -> list[dict]:
    """Screen a ClassificationResult in place; returns the flags raised.

    Hard rule downgrades mutate the offending dishes (verdict -> unclear,
    confidence capped) so a bad verdict never reaches the database at all.
    Soft rules only report. Each flag dict is ready for db.record_audits.
    """
    flags: list[dict] = []

    for dish in result.dishes:
        if dish.verdict not in ("vegan", "likely_vegan"):
            continue
        text = _dish_text(dish)
        match = _ANIMAL_RE.search(text)
        if match and not _mock_qualified(text):
            flags.append({
                "check_type": "guardrail",
                "rule": "animal_ingredient_vegan",
                "dish_name": dish.name,
                "status": "downgraded",
                "detail": (
                    f"'{match.group(0)}' present but verdict was "
                    f"{dish.verdict} ({dish.confidence:.2f}); "
                    "downgraded to unclear"
                ),
                "expected_verdict": None,
                "actual_verdict": dish.verdict,
            })
            dish.verdict = "unclear"
            dish.confidence = min(dish.confidence, 0.3)
            dish.reasoning = (
                (dish.reasoning + " " if dish.reasoning else "")
                + "[guardrail: animal ingredient named; needs review]"
            ).strip()

    food = [d for d in result.dishes if d.category == "food"]
    if len(food) >= 10:
        vegan_rate = sum(
            1 for d in food if d.verdict in ("vegan", "likely_vegan")
        ) / len(food)
        if vegan_rate > 0.8:
            flags.append({
                "check_type": "guardrail",
                "rule": "implausible_vegan_rate",
                "dish_name": None,
                "status": "flagged",
                "detail": (
                    f"{vegan_rate:.0%} of {len(food)} food dishes marked "
                    "vegan/likely_vegan — plausible only for a vegan "
                    "restaurant; spot-check this run"
                ),
                "expected_verdict": None,
                "actual_verdict": None,
            })

    confidences = [d.confidence for d in result.dishes]
    if len(confidences) >= 10 and statistics.pstdev(confidences) < 0.01:
        flags.append({
            "check_type": "guardrail",
            "rule": "uniform_confidence",
            "dish_name": None,
            "status": "flagged",
            "detail": (
                f"All {len(confidences)} confidences within 0.01 of each "
                "other — the model is not actually calibrating"
            ),
            "expected_verdict": None,
            "actual_verdict": None,
        })

    return flags
