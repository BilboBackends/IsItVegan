"""The non-negotiable safety rule: no vegan verdict on unqualified animal
words — found live when Athena's "Grilled Chicken Pita" was stored as vegan
(the model classified the description, which omitted the name's chicken,
and the run-level guardrail screen was switched off via env).

Also pins the precision layer (nut butters, steak fries, wine producers are
NOT animal ingredients) and the plant-based-venue exemption ("Grilled
Cheese" at a fully vegan restaurant is vegan cheese by definition).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import classifier  # noqa: E402
from guardrails import (  # noqa: E402
    defining_animal_ingredient,
    is_plant_based_venue,
    menu_declares_dish_vegan,
    unqualified_drink_animal_ingredient,
    unqualified_animal_word,
)


def _payload(name, description, verdict="vegan", confidence=0.9):
    return {
        "dishes": [
            {
                "name": name,
                "description": description,
                "price": None,
                "calories": None,
                "category": "food",
                "verdict": verdict,
                "confidence": confidence,
                "reasoning": "Plant-based ingredients.",
                "evidence": description or name,
                "dairy_status": "free",
                "gluten_status": "unclear",
                "nut_status": "unclear",
                "protein_level": "moderate",
                "serving_role": "meal",
                "alcohol_status": "unclear",
                "meal_types": ["lunch"],
                "key_ingredients": [],
            }
        ]
    }


def _classify(name, description, verdict="vegan", **kwargs):
    result = classifier.result_from_data(
        _payload(name, description, verdict=verdict),
        provider="deepseek",
        model="deepseek-v4-flash",
        billing="deepseek_api",
        **kwargs,
    )
    return result.dishes[0]


# ---- the Athena case: unconditional, no env flag can disable it ------------

def test_chicken_pita_can_never_be_stored_vegan():
    dish = _classify(
        "Grilled Chicken Pita", "Taboule, lettuce, and hummus in a grilled pita"
    )
    assert dish.verdict == "unclear"
    assert dish.confidence <= 0.4
    assert "Chicken" in dish.reasoning or "chicken" in dish.reasoning


def test_animal_in_description_also_downgrades():
    dish = _classify("Garden Wrap", "lettuce, tomato, ranch dressing")
    assert dish.verdict == "unclear"


def test_mock_qualified_dishes_stay_vegan():
    dish = _classify("Vegan Chicken Pita", "seitan chicken, lettuce, hummus")
    assert dish.verdict == "vegan"


def test_explicit_whole_dish_vegan_description_stays_vegan():
    dish = _classify(
        "Oatmeal Cream Pie", "Vegan and gluten-free oatmeal cream pie cookie."
    )
    assert dish.verdict == "vegan"
    assert menu_declares_dish_vegan("Vegan and gluten-free cookie")


def test_unrelated_plant_word_does_not_excuse_chicken():
    dish = _classify(
        "Grilled Chicken Pita", "chicken with plant-based hummus and lettuce"
    )
    assert dish.verdict == "unclear"


# ---- precision: these are NOT animal ingredients ----------------------------

def test_plant_compounds_are_not_flagged():
    for name in (
        "Peanut Butter",
        "Cookie Butter Shake",
        "Butter Beans",
        "Butternut Squash Soup",
        "Steak Fries",
        "Cauliflower Steak",
        "IBC Cream Soda",
        "Wild Turkey 101",
        "Ranch Water",
        "Oyster Bay Sauvignon Blanc",
        "Baingan (Egg Plant) Bhurta",
        'Heart of Palm "Crab Cakes"',
        "Non Dairy Whipped Cream Cheese",
        "Coconut Milk Custard",
    ):
        assert not unqualified_animal_word(name), name
        assert defining_animal_ingredient(name, name) is None, name


def test_negated_ingredients_are_not_flagged():
    for text in (
        "Arepa sin Queso",
        "Everything Bagel, no cream cheese",
        "Garden bowl without cheese",
        "Pancakes that are dairy and egg free",
        "Tofu sub does not have pate and butter",
        "Guacamole (Add cheese +$2)",
    ):
        assert not unqualified_animal_word(text), text


def test_real_animal_ingredients_still_flag():
    for name, text in (
        ("Grilled Chicken Pita", "Grilled Chicken Pita taboule hummus"),
        ("Carrot Sticks & Ranch Dressing", "Carrot Sticks & Ranch Dressing"),
        ("Buffalo Caprese", "Buffalo Caprese tomatoes basil"),
        ("Honey Rooibos", "Honey Rooibos tea"),
    ):
        assert unqualified_animal_word(text), name


def test_drink_names_ignore_brand_wordplay_but_not_honey():
    for name in (
        "Cabernet Sauvignon, Meyers Ranch",
        "Gone Fish’n",
        "Tox Eel-ectric Peach Lemonade",
        "Harvey's Cream Sherry",
    ):
        assert unqualified_drink_animal_ingredient(name) is None
    assert unqualified_drink_animal_ingredient("Honey Rooibos") == "honey"


# ---- plant-based venues: substitutes keep their verdicts --------------------

def test_plant_based_venue_exempts_substitute_names():
    dish = _classify(
        "KIDS GRILLED CHEESE MEAL",
        "melty cheese on toasted bread",
        plant_based_venue=True,
    )
    assert dish.verdict == "vegan"


def test_is_plant_based_venue_detection():
    assert is_plant_based_venue("Karelyn's Vegan Kitchen")
    assert is_plant_based_venue(
        "Plantees", "A completely vegan restaurant.", None,
    )
    assert is_plant_based_venue(
        "VEGGIE GARDEN", None, "Our menu is 100% vegan.",
    )
    assert is_plant_based_venue("Winter Park Biscuit Company")
    assert is_plant_based_venue("Some Cafe", "A 100% vegan diner.", None)
    # Vegetarian/veggie branding does not prove the absence of dairy/eggs,
    # and an unrelated business name containing "plant" proves nothing.
    assert not is_plant_based_venue("Vegetarian Palace")
    assert not is_plant_based_venue("Plant Street Market")
    # An ordinary steakhouse mentioning a vegan option does NOT qualify.
    assert not is_plant_based_venue(
        "Prime Steakhouse", None, "vegan options available on request"
    )
