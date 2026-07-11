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
        calories="520-610 cal",
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
    assert dish["calories"] == "520-610 cal"
    assert dish["meal_types"] == ["breakfast", "brunch"]
    assert dish["key_ingredients"] == ["tofu", "black bean", "mushroom"]

    searchable = db.list_all_dishes(path)[0]
    assert searchable["gluten_status"] == "free"
    assert searchable["calories"] == "520-610 cal"
    assert searchable["key_ingredients"] == ["tofu", "black bean", "mushroom"]


def test_mock_classifier_populates_future_search_attributes():
    result = classifier.classify_menu("menu", restaurant_name="Cafe", mock=True)

    assert result.ok
    dish = result.dishes[0]
    assert dish.dairy_status == "free"
    assert dish.protein_level == "moderate"
    assert dish.calories == "420 cal"
    assert "chickpea" in dish.key_ingredients


def test_provider_output_uses_shared_validation(monkeypatch):
    payload = {
        "dishes": [
            {
                "name": "  Tofu Plate  ",
                "description": "tofu and mushrooms",
                "price": "$12",
                "calories": "480 cal",
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
    assert result.dishes[0].calories == "480 cal"
    assert result.dishes[0].key_ingredients == ["tofu", "mushroom"]


def test_deepseek_full_classification_proactively_chunks_long_menus(monkeypatch):
    calls = []

    def payload(name):
        return {
            "name": name,
            "description": "tofu and mushrooms",
            "price": "$12",
            "calories": None,
            "category": "food",
            "verdict": "vegan",
            "confidence": 0.9,
            "reasoning": "Listed ingredients are plant-based.",
            "evidence": "tofu and mushrooms",
            "dairy_status": "free",
            "gluten_status": "free",
            "nut_status": "free",
            "protein_level": "moderate",
            "serving_role": "meal",
            "alcohol_status": "unclear",
            "meal_types": ["lunch"],
            "key_ingredients": ["tofu", "mushroom"],
        }

    def fake_run_provider(**kwargs):
        calls.append(kwargs["user_prompt"])
        assert "ONE SECTION of a larger menu" in kwargs["user_prompt"]
        return ProviderResponse(
            ok=True,
            provider="deepseek",
            model="deepseek-test",
            billing="deepseek_api",
            data={"dishes": [payload(f"Tofu Plate {len(calls)}")]},
        )

    monkeypatch.setattr(classifier, "run_provider", fake_run_provider)
    long_menu = "\n".join(
        f"Tofu Plate {i}\ntofu and mushrooms\n${i}.00"
        for i in range(1, 500)
    )

    result = classifier.classify_menu(
        long_menu, restaurant_name="Cafe", provider="deepseek"
    )

    assert result.ok
    assert len(calls) > 1
    assert len(result.dishes) == len(calls)


def test_add_on_items_are_forced_to_side_not_meal():
    result = classifier.result_from_data(
        {
            "dishes": [
                {
                    "name": "Add Grilled Chicken",
                    "description": "Add to your salad",
                    "price": "$8",
                    "calories": None,
                    "category": "food",
                    "verdict": "not_vegan",
                    "confidence": 0.97,
                    "reasoning": "Chicken is animal protein.",
                    "evidence": "Grilled Chicken",
                    "dairy_status": "free",
                    "gluten_status": "free",
                    "nut_status": "free",
                    "protein_level": "high",
                    "serving_role": "meal",
                    "alcohol_status": "unclear",
                    "meal_types": ["lunch", "dinner"],
                    "key_ingredients": ["chicken"],
                }
            ]
        },
        provider="codex",
        model="codex-test",
        billing="chatgpt_subscription",
    )

    assert result.ok
    assert result.dishes[0].serving_role == "side"


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
    assert counts["vegan_meals"] == 1
    assert counts["vegan_sides"] == 1


def test_headline_vegan_counts_are_strict(tmp_path):
    # vegan_adaptable NEVER counts as vegan on a card, and likely_vegan only
    # counts above the confidence bar — being transparently uncertain beats
    # being confidently wrong (CLAUDE.md).
    path = str(tmp_path / "strict_counts.db")
    db.init_db(path)
    restaurant_id = _restaurant(path)

    def add(name, verdict, confidence, serving_role="meal"):
        dish_id = db.upsert_dish(
            restaurant_id, name, None, None, category="food", db_path=path
        )
        db.insert_classification(
            dish_id=dish_id,
            verdict=verdict,
            confidence=confidence,
            reasoning="test",
            source_id=None,
            model_version="test",
            created_at="2026-07-04T12:00:00+00:00",
            serving_role=serving_role,
            db_path=path,
        )

    add("Certain Vegan Bowl", "vegan", 0.95)              # counts
    add("Confident Likely Curry", "likely_vegan", 0.80)   # counts (>= 0.75)
    add("Shaky Likely Soup", "likely_vegan", 0.55)        # too uncertain
    add("Hold-The-Cheese Wrap", "vegan_adaptable", 0.95)  # never counts
    add("Cheese Pizza", "not_vegan", 0.99)                # never counts
    add("Vegan Fries", "vegan", 0.9, serving_role="side")  # counts as side

    counts = db.verdict_counts_by_restaurant(path)[restaurant_id]
    assert counts["vegan_meals"] == 2
    assert counts["vegan_sides"] == 1
    # The full distribution stays available for detail views.
    assert counts["by_verdict"]["vegan_adaptable"] == 1
    assert counts["by_verdict"]["likely_vegan"] == 2
