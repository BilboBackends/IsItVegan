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
    "provolone", "gouda", "brie", "queso", "burrata", "halloumi", "manchego",
    "butter", "cream", "milk", "yogurt", "ghee", "custard", "gelato",
    "egg", "eggs", "mayo", "mayonnaise", "aioli", "ranch", "alfredo",
    "honey", "gelatin", "lard", "tallow", "whey",
)

# A nearby mock/plant qualifier means the animal word is a plant version
# ("vegan cheese", "soy chorizo", "Impossible beef") — no flag.
_MOCK_WORDS = (
    "vegan", "plant-based", "plant based", "plant base", "dairy-free", "dairy free",
    "non-dairy", "non dairy", "meatless", "mock", "imitation", "impossible", "beyond",
    "tofu", "tempeh", "seitan", "soy", "cashew", "almond", "oat", "coconut",
    "nutritional yeast", "vegetable broth", "veggie", "jackfruit",
    "substitute", "alternative", "faux", "daiya", "pb", "heart of palm",
    "bean curd", "chick'n", "chikn", "no-egg",
    "eggless", "aquafaba", "flax",
)

_ANIMAL_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _ANIMAL_WORDS) + r")\b",
    re.IGNORECASE,
)

# Phrases that CONTAIN an animal word but are not animal ingredients — nut
# butters, steak-cut potatoes, sodas, and producer names on drink lists.
# Found by sweeping 12k stored verdicts: without these the screen flags
# "Peanut Butter", "Steak Fries", "Wild Turkey 101" (bourbon), "Oyster Bay
# Sauvignon Blanc" (winery), "IBC Cream Soda"... Stripped before matching.
_PLANT_COMPOUNDS = (
    "cream of coconut", "coconut cream", "coconut milk", "coconut yogurt",
    "coconut milk custard", "cream of hearts of palm",
    "soy milk base cream", "oat milk", "soy milk", "almond milk",
    "peanut butter", "almond butter", "cashew butter", "sunflower butter",
    "sun butter", "sunbutter", "seed butter", "cookie butter", "apple butter",
    "cocoa butter", "shea butter", "butter bean", "butter beans",
    "butternut", "butter lettuce",
    "steak fries", "steak-cut", "steak cut", "cauliflower steak",
    "mushroom steak", "watermelon steak",
    "cream soda", "cream ale",
    "egg plant", "eggplant", "just egg",
    "oyster mushroom", "oyster mushrooms", "king oyster", "bean curd sausage",
    # Spirit/producer names, not ingredients.
    "wild turkey", "ranch water", "oyster bay", "billecart salmon",
    "cream sherry", "heart of palm crab", "just in queso foundation",
    "cheddar style vegan cheese",
    "santa margherita", "grappa bianca",
)

# "no cheese", "without cream", "sin queso", "hold the mayo": the menu is
# REMOVING the ingredient — strip the whole negated phrase before matching.
_NEGATED_ANIMAL_RE = re.compile(
    r"\b(?:no|without|sin|senza|hold(?:\s+the)?|does\s+not\s+have)\s+"
    r"(?:[\w'-]+\s+){0,2}?(?:"
    + "|".join(re.escape(w) for w in _ANIMAL_WORDS)
    + r")(?:\s+(?:"
    + "|".join(re.escape(w) for w in _ANIMAL_WORDS)
    + r"))*\b",
    re.IGNORECASE,
)

_ANIMAL_FREE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in _ANIMAL_WORDS)
    + r")(?:\s+(?:and|/)\s+(?:"
    + "|".join(re.escape(w) for w in _ANIMAL_WORDS)
    + r"))*[- ]free\b",
    re.IGNORECASE,
)

# These clauses describe optional variants, not the base dish being judged.
_OPTIONAL_VARIANT_RE = re.compile(
    r"\b(?:also\s+)?available\s+with\b[^.;]*"
    r"|\(?\badd(?:ed)?\b[^().;]*(?:"
    + "|".join(re.escape(w) for w in _ANIMAL_WORDS)
    + r")[^().;]*\)?"
    r"|\boptional\b[^.;]*"
    r"|\btry\s+any\s+one\s+of\b[^.;]*"
    r"|\bdoes\s+not\s+have\b[^.;]*"
    r"|\btop\s+any\b[^.;]*"
    r"|[|—–-]\s+[^|.;]*\+\$\d+(?:\.\d+)?",
    re.IGNORECASE,
)


def _screenable(text: str) -> str:
    """Text with plant compounds and negated ingredients removed, so the
    animal-word regexes only ever see genuinely risky mentions."""
    lowered = (text or "").lower()
    lowered = re.sub(r"[\"“”'‘’]+", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered)
    for phrase in sorted(_PLANT_COMPOUNDS, key=len, reverse=True):
        lowered = lowered.replace(phrase, " ")
    lowered = _NEGATED_ANIMAL_RE.sub(" ", lowered)
    lowered = _ANIMAL_FREE_RE.sub(" ", lowered)
    lowered = _OPTIONAL_VARIANT_RE.sub(" ", lowered)
    return _QUALIFIED_RISK_RE.sub(" ", lowered)


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

# Remove only a qualifier that directly modifies a risky term. The previous
# global check let an unrelated phrase such as "plant-based hummus" excuse
# "Chicken Pita" elsewhere in the same description.
_RISK_TERMS = _ANIMAL_WORDS + _ANIMAL_DEFINED_DISHES
_QUALIFIED_RISK_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(w) for w in sorted(_MOCK_WORDS, key=len, reverse=True))
    + r")(?:[\s™®-]+[\w'-]+){0,3}[\s™®-]+(?:"
    + "|".join(re.escape(w) for w in sorted(_RISK_TERMS, key=len, reverse=True))
    + r")(?:[\s-]+(?:"
    + "|".join(re.escape(w) for w in sorted(_RISK_TERMS, key=len, reverse=True))
    + r"))*\b",
    re.IGNORECASE,
)


# Strong all-vegan venue markers. At a fully plant-based restaurant the
# animal-word screens are all wrong — their "Grilled Cheese", "Sausage
# Gravy", and "Chicken" are substitutes by definition (Plantees, VEGGIE
# GARDEN, Winter Park Biscuit Co). A lone "vegan options available" on an
# ordinary menu must NOT qualify, so these are deliberately strong phrases.
_PLANT_VENUE_NAME_RE = re.compile(r"\b(?:vegan|plant[- ]based)\b", re.IGNORECASE)

# Exact venues whose official sites explicitly state that the entire menu is
# plant-based. Exact matching avoids treating generic "veggie" or "plant"
# business names as vegan. Re-verify if one of these businesses changes hands.
_VERIFIED_PLANT_VENUE_NAMES = frozenset({
    "plantees",
    "veggie garden",
    "winter park biscuit company",
})
_PLANT_VENUE_TEXT_MARKERS = (
    "100% vegan", "100% plant-based", "100% plant based", "all vegan",
    "fully vegan", "completely vegan", "entirely plant-based",
    "entirely plant based", "vegan restaurant", "vegan cafe", "vegan cafe",
    "vegan bakery", "vegan kitchen", "vegan comfort food", "vegan diner",
    "plant-based restaurant", "plant based restaurant", "plant-based menu",
    "plant based menu", "everything is vegan", "menu is vegan",
)


def is_plant_based_venue(
    restaurant_name: str | None,
    editorial_summary: str | None = None,
    menu_text: str | None = None,
) -> bool:
    """Whether this restaurant is (very likely) fully plant-based, in which
    case animal words on its menu are substitutes and every animal-word
    screen must stand down."""
    name = (restaurant_name or "").lower()
    normalized_name = re.sub(r"[^a-z0-9]+", " ", name).strip()
    if (
        normalized_name in _VERIFIED_PLANT_VENUE_NAMES
        or _PLANT_VENUE_NAME_RE.search(name)
    ):
        return True
    context = f"{editorial_summary or ''} {menu_text or ''}".lower()
    return any(marker in context for marker in _PLANT_VENUE_TEXT_MARKERS)


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
    screenable_name = _screenable(name)
    match = _ANIMAL_RE.search(screenable_name) or _ANIMAL_DEFINED_RE.search(
        screenable_name
    )
    if match:
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


def unqualified_animal_ingredient(text: str) -> str | None:
    """The first unqualified animal/animal-defined term, if one exists."""
    screenable = _screenable(text)
    match = _ANIMAL_RE.search(screenable) or _ANIMAL_DEFINED_RE.search(screenable)
    return match.group(0) if match else None


def unqualified_animal_word(text: str) -> bool:
    """True when menu text contains an unqualified animal-risk term."""
    return unqualified_animal_ingredient(text) is not None


_DRINK_ANIMAL_WORDS = (
    "honey", "milk", "yogurt", "egg", "eggs", "gelatin", "lard", "tallow",
    "whey", "custard", "gelato",
)
_DRINK_ANIMAL_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _DRINK_ANIMAL_WORDS) + r")\b",
    re.IGNORECASE,
)


def unqualified_drink_animal_ingredient(text: str) -> str | None:
    """High-signal animal terms in a drink name.

    Cocktail/wine names routinely contain ranch, fish, eel, butter, and cream
    as brands, wordplay, or tasting styles. Only direct ingredient-like terms
    are safe enough for a deterministic name backstop.
    """
    match = _DRINK_ANIMAL_RE.search(_screenable(text))
    return match.group(0) if match else None


_WHOLE_DISH_VEGAN_RE = re.compile(
    r"^\s*vegan\b|\b(?:certified|explicitly)\s+vegan\b",
    re.IGNORECASE,
)


def menu_declares_dish_vegan(description: str | None) -> bool:
    """Whether the restaurant explicitly labels the whole described item vegan."""
    return bool(_WHOLE_DISH_VEGAN_RE.search(description or ""))


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
        match = _ANIMAL_RE.search(_screenable(text))
        if match:
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
