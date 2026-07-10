"""Alcohol labeling: word-list rules + the classifier validation backstop.

A Coke and a tequila are not the same kind of "drink" — the Drinks tab
sections on alcohol_status, so the label has to be right for obvious names
regardless of which model classified the dish.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alcohol import classify_alcohol  # noqa: E402
from classifier import result_from_data  # noqa: E402


def test_word_rules_cover_the_obvious_cases():
    assert classify_alcohol("Coca-Cola") == "non_alcoholic"
    assert classify_alcohol("Fresh Squeezed Lemonade") == "non_alcoholic"
    assert classify_alcohol("Oat Milk Latte") == "non_alcoholic"
    assert classify_alcohol("House Margarita, salt rim") == "alcoholic"
    assert classify_alcohol("Hazy IPA (draft)") == "alcoholic"
    assert classify_alcohol("Casamigos Tequila flight") == "alcoholic"
    assert classify_alcohol("Seasonal beverage") == "unclear"


def test_zero_proof_markers_beat_cocktail_names():
    assert classify_alcohol("Virgin Margarita") == "non_alcoholic"
    assert classify_alcohol("Zero-proof Negroni") == "non_alcoholic"
    assert classify_alcohol("Mocktail of the day") == "non_alcoholic"
    # ...but a coffee-flavored cocktail is still a cocktail.
    assert classify_alcohol("Espresso Martini") == "alcoholic"


def _drink(name, alcohol_status=None):
    dish = {
        "name": name, "description": None, "price": "$5", "calories": None,
        "category": "drink", "verdict": "not_vegan", "confidence": 0.8,
        "reasoning": "x", "evidence": "", "dairy_status": "unclear",
        "gluten_status": "unclear", "nut_status": "unclear",
        "protein_level": "unclear", "serving_role": "unclear",
        "meal_types": [], "key_ingredients": [],
    }
    if alcohol_status is not None:
        dish["alcohol_status"] = alcohol_status
    return dish


def test_validation_backstop_labels_drinks_the_model_left_unclear():
    result = result_from_data(
        {"dishes": [
            _drink("Mexican Coke"),                      # no field at all
            _drink("House Cabernet", "unclear"),         # model punted
            _drink("Mystery Punch"),                     # genuinely unclear
            _drink("Draft Beer", "alcoholic"),           # model value kept
        ]},
        provider="deepseek", model="m", billing="x",
    )
    statuses = {d.name: d.alcohol_status for d in result.dishes}
    assert statuses["Mexican Coke"] == "non_alcoholic"
    assert statuses["House Cabernet"] == "alcoholic"
    assert statuses["Mystery Punch"] == "unclear"
    assert statuses["Draft Beer"] == "alcoholic"
