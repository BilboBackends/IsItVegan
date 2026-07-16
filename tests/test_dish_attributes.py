"""Second-pass attribute enrichment: validation, work selection, storage."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import dish_attributes  # noqa: E402


def _restaurant(path: str) -> int:
    db.upsert_restaurants(
        [{"name": "Attr Cafe", "place_id": "attr-1", "website_url": "https://x.com"}],
        path,
    )
    return db.list_restaurants(path)[0]["id"]


def _dish(path: str, restaurant_id: int, name: str, category: str = "food") -> int:
    dish_id = db.upsert_dish(
        restaurant_id, name, "beans, rice, salsa", "$10",
        category=category, db_path=path,
    )
    db.insert_classification(
        dish_id=dish_id,
        verdict="vegan_adaptable",
        confidence=0.8,
        reasoning="Cheese can be dropped.",
        source_id=None,
        model_version="test",
        created_at="2026-07-16T00:00:00+00:00",
        db_path=path,
    )
    return dish_id


def _valid_attrs(dish_id: int) -> dict:
    return {
        "id": dish_id,
        "vegetarian_status": "vegetarian",
        "protein_source": "legume",
        "egg_status": "free",
        "soy_status": "unclear",
        "sesame_status": "unclear",
        "spice_level": "mild",
        "cooking_method": "unclear",
        "dish_format": "burrito",
        "meat_sources": [],
        "flavor_profile": ["savory"],
        "ingredient_tags": ["Bean ", "rice", "salsa"],
        "adaptation": "Ask for no cheese.",
    }


def test_validate_rows_accepts_good_and_drops_bad_individually():
    batch = [{"dish_id": 1}, {"dish_id": 2}, {"dish_id": 3}]
    good = {**_valid_attrs(1), "meat_sources": ["pork", "pork", "fish"]}
    bad = {**_valid_attrs(2), "dish_format": "burrito_bowl_supreme"}
    accepted, problems = dish_attributes.validate_rows(
        batch, {"dishes": [good, bad]}
    )
    assert set(accepted) == {1}
    # Tags are canonicalized to trimmed lowercase; meats are deduped.
    assert accepted[1]["ingredient_tags"] == ["bean", "rice", "salsa"]
    assert accepted[1]["meat_sources"] == ["pork", "fish"]
    assert accepted[1]["adaptation"] == "Ask for no cheese."
    assert any("dish 2" in p for p in problems)
    assert any("dish 3: missing" in p for p in problems)


def test_validate_rows_rejects_unknown_ids_and_bad_shapes():
    accepted, problems = dish_attributes.validate_rows(
        [{"dish_id": 1}], {"dishes": [{**_valid_attrs(99)}]}
    )
    assert accepted == {}
    assert any("unexpected id: 99" in p for p in problems)
    accepted, problems = dish_attributes.validate_rows([{"dish_id": 1}], {})
    assert accepted == {} and problems == ["response has no dishes array"]


def test_pending_work_skips_drinks_and_enriched_rows(tmp_path):
    path = str(tmp_path / "attrs.db")
    db.init_db(path)
    restaurant_id = _restaurant(path)
    food_id = _dish(path, restaurant_id, "Bean Burrito")
    _dish(path, restaurant_id, "House Lager", category="drink")

    work = dish_attributes.pending_work(path)
    assert [row["dish_id"] for row in work] == [food_id]
    assert work[0]["verdict"] == "vegan_adaptable"

    stored = dish_attributes.store_attributes(
        work,
        {food_id: dish_attributes.validate_rows(
            work, {"dishes": [_valid_attrs(food_id)]}
        )[0][food_id]},
        model="deepseek-test",
        db_path=path,
    )
    assert stored == 1
    assert dish_attributes.pending_work(path) == []

    with db.connect(path) as conn:
        row = conn.execute(
            """
            SELECT vegetarian_status, protein_source, dish_format,
                   flavor_profile, ingredient_tags, vegan_adaptation,
                   attributes_enriched_at, attributes_model, meat_sources
            FROM classifications WHERE dish_id = ?
            """,
            (food_id,),
        ).fetchone()
    assert row[0] == "vegetarian"
    assert row[1] == "legume"
    assert row[2] == "burrito"
    assert json.loads(row[3]) == ["savory"]
    assert json.loads(row[4]) == ["bean", "rice", "salsa"]
    assert row[5] == "Ask for no cheese."
    assert row[6] and row[7] == "deepseek-test"
    assert json.loads(row[8]) == []
