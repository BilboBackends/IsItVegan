"""Dietary/discovery attributes are persisted with each dish classification."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import classifier  # noqa: E402
from classification_providers import ProviderResponse  # noqa: E402
import db  # noqa: E402


def _restaurant(path: str) -> int:
    db.upsert_restaurants(
        [{"name": "Search Cafe", "place_id": "search-1", "website_url": "https://x.com"}],
        path,
    )
    return db.list_restaurants(path)[0]["id"]


def test_dietary_attributes_round_trip_through_dish_reads(tmp_path):
    path = str(tmp_path / "attributes.db")
    db.init_db(path)
    restaurant_id = _restaurant(path)
    dish_id = db.upsert_dish(
        restaurant_id,
        "Tofu Breakfast Bowl",
        "tofu, black beans, mushroom",
        "$14",
        category="food",
        db_path=path,
    )
    db.insert_classification(
        dish_id=dish_id,
        verdict="vegan",
        confidence=0.94,
        reasoning="Plant-based protein and vegetables.",
        source_id=None,
        model_version="test",
        created_at="2026-07-03T14:30:00+00:00",
        dairy_status="free",
        gluten_status="free",
        nut_status="free",
        protein_level="high",
        serving_role="meal",
        meal_types=["breakfast", "brunch"],
        key_ingredients=["tofu", "black bean", "mushroom"],
        db_path=path,
    )

    dish = db.list_dishes(restaurant_id, path)[0]
    assert dish["dairy_status"] == "free"
    assert dish["protein_level"] == "high"
    assert dish["serving_role"] == "meal"
    assert dish["meal_types"] == ["breakfast", "brunch"]
    assert dish["key_ingredients"] == ["tofu", "black bean", "mushroom"]

    searchable = db.list_all_dishes(path)[0]
    assert searchable["gluten_status"] == "free"
    assert searchable["key_ingredients"] == ["tofu", "black bean", "mushroom"]


def test_mock_classifier_populates_future_search_attributes():
    result = classifier.classify_menu("menu", restaurant_name="Cafe", mock=True)

    assert result.ok
    dish = result.dishes[0]
    assert dish.dairy_status == "free"
    assert dish.protein_level == "moderate"
    assert "chickpea" in dish.key_ingredients


def test_provider_output_uses_shared_validation(monkeypatch):
    payload = {
        "dishes": [
            {
                "name": "  Tofu Plate  ",
                "description": "tofu and mushrooms",
                "price": "$12",
                "category": "food",
                "verdict": "vegan",
                "confidence": 1.4,
                "reasoning": "Plant ingredients.",
                "evidence": "tofu and mushrooms",
                "dairy_status": "free",
                "gluten_status": "unclear",
                "nut_status": "free",
                "protein_level": "high",
                "serving_role": "meal",
                "meal_types": ["lunch", "dinner"],
                "key_ingredients": ["Tofu", "Mushroom"],
            }
        ]
    }
    monkeypatch.setattr(
        classifier,
        "run_provider",
        lambda **kwargs: ProviderResponse(
            ok=True,
            provider="codex",
            model="codex-test",
            billing="chatgpt_subscription",
            data=payload,
        ),
    )

    result = classifier.classify_menu(
        "menu", restaurant_name="Cafe", provider="codex"
    )

    assert result.ok
    assert result.provider == "codex"
    assert result.billing == "chatgpt_subscription"
    assert result.dishes[0].name == "Tofu Plate"
    assert result.dishes[0].confidence == 1.0
    assert result.dishes[0].serving_role == "meal"
    assert result.dishes[0].key_ingredients == ["tofu", "mushroom"]


def test_restaurant_counts_split_meals_and_sides_and_exclude_other_categories(tmp_path):
    path = str(tmp_path / "serving_roles.db")
    db.init_db(path)
    restaurant_id = _restaurant(path)

    def add(name: str, category: str, serving_role: str) -> None:
        dish_id = db.upsert_dish(
            restaurant_id, name, None, None, category=category, db_path=path
        )
        db.insert_classification(
            dish_id=dish_id,
            verdict="vegan",
            confidence=0.9,
            reasoning="test",
            source_id=None,
            model_version="test",
            created_at="2026-07-04T12:00:00+00:00",
            serving_role=serving_role,
            db_path=path,
        )

    add("Tofu Bowl", "food", "meal")
    add("Side Salad", "food", "side")
    add("Brownie", "dessert", "unclear")
    add("Lemonade", "drink", "unclear")

    counts = db.verdict_counts_by_restaurant(path)[restaurant_id]
    assert counts["total"] == 4
    assert counts["by_verdict"] == {"vegan": 1}
    assert counts["sides_by_verdict"] == {"vegan": 1}
