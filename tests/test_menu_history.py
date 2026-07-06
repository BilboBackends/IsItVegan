"""Tests for menu version history, dish-change tracking, and delta support.

The recrawl story: menus drift, so every distinct capture is stored as an
immutable version, reclassification skips unchanged menus entirely, delta
runs touch only the dishes that changed, and every transition leaves a
dish_changes record (added / removed / price_changed / verdict_changed).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import db  # noqa: E402
from classifier import result_from_data  # noqa: E402


@pytest.fixture()
def test_db(tmp_path):
    path = str(tmp_path / "history.db")
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO restaurants (id, name, place_id) VALUES (1, 'Cafe', 'p1')"
        )
    return path


def test_menu_versions_dedupe_identical_recrawls(test_db):
    assert db.record_menu_version(1, "menu v1", "hash-a", db_path=test_db) is True
    assert db.record_menu_version(1, "menu v1", "hash-a", db_path=test_db) is False
    assert db.record_menu_version(1, "menu v2", "hash-b", db_path=test_db) is True
    versions = db.list_menu_versions(1, db_path=test_db)
    assert [v["content_hash"] for v in versions] == ["hash-b", "hash-a"]
    # Content only ships when asked for.
    assert "content" not in versions[0]
    full = db.list_menu_versions(1, include_content=True, db_path=test_db)
    assert full[1]["content"] == "menu v1"


def test_compute_dish_changes_covers_all_transition_types():
    prior = {
        "Tofu Bowl": {"price": "$10", "verdict": "vegan"},
        "Old Special": {"price": "$12", "verdict": "likely_vegan"},
        "Fries": {"price": "$4", "verdict": "vegan"},
        "Curry": {"price": "$11", "verdict": "unclear"},
    }
    current = {
        "Tofu Bowl": {"price": "$12", "verdict": "vegan"},       # price up
        "Fries": {"price": "$4", "verdict": "vegan"},            # unchanged
        "Curry": {"price": "$11", "verdict": "likely_vegan"},    # verdict moved
        "New Wrap": {"price": "$9", "verdict": "vegan"},         # added
    }
    changes = {
        (c["change_type"], c["dish_name"]): c
        for c in db.compute_dish_changes(prior, current)
    }
    assert ("price_changed", "Tofu Bowl") in changes
    assert changes[("price_changed", "Tofu Bowl")]["new_price"] == "$12"
    assert ("verdict_changed", "Curry") in changes
    assert ("added", "New Wrap") in changes
    assert ("removed", "Old Special") in changes
    assert len(changes) == 4  # unchanged Fries produces nothing


def test_record_and_list_dish_changes(test_db):
    db.record_dish_changes(
        1,
        [
            {"change_type": "added", "dish_name": "New Wrap", "new_price": "$9"},
            {"change_type": "removed", "dish_name": "Old Special",
             "old_price": "$12"},
        ],
        observed_at="2026-07-05T00:00:00+00:00",
        db_path=test_db,
    )
    changes = db.list_dish_changes(1, db_path=test_db)
    assert len(changes) == 2
    assert {c["change_type"] for c in changes} == {"added", "removed"}


def test_delete_dish_preserves_reports(test_db):
    dish_id = db.upsert_dish(1, "Doomed Dish", None, "$5", db_path=test_db)
    with db.connect(test_db) as conn:
        conn.execute(
            """
            INSERT INTO reports (restaurant_id, dish_id, dish_name, issue_type,
                                 status, created_at)
            VALUES (1, ?, 'Doomed Dish', 'other', 'open', '2026-07-05')
            """,
            (dish_id,),
        )
    assert db.delete_dish(1, "Doomed Dish", db_path=test_db) is True
    assert db.delete_dish(1, "Doomed Dish", db_path=test_db) is False
    with db.connect(test_db) as conn:
        report = conn.execute("SELECT dish_id, dish_name FROM reports").fetchone()
    assert report["dish_id"] is None
    assert report["dish_name"] == "Doomed Dish"


def test_delta_result_allows_empty_dishes_and_carries_removed_names():
    result = result_from_data(
        {"dishes": [], "removed_dish_names": ["Old Special", "  ", "Gone Soup"]},
        provider="claude",
        model="m",
        billing="claude_subscription",
        mode="delta",
    )
    assert result.ok  # "nothing new" is a valid delta answer
    assert result.mode == "delta"
    assert result.removed_dish_names == ["Old Special", "Gone Soup"]


def test_full_mode_still_rejects_empty_dishes():
    result = result_from_data(
        {"dishes": []}, provider="claude", model="m", billing="x"
    )
    assert not result.ok


def test_last_classified_hash_round_trip(test_db):
    db.set_last_classified_hash(1, "abc123", db_path=test_db)
    rows = db.list_restaurants(test_db)
    assert rows[0]["last_classified_hash"] == "abc123"


def test_upsert_dish_collapses_case_and_spacing_duplicate(test_db):
    first = db.upsert_dish(
        1, "Earth CrisisⓋ", "Red sauce and mushrooms", "$18.00", db_path=test_db
    )
    second = db.upsert_dish(
        1, "EARTH CRISIS Ⓥ", "Red sauce and mushrooms", "$18.00", db_path=test_db
    )

    assert second == first
    dishes = db.list_dishes(1, test_db)
    assert [dish["name"] for dish in dishes] == ["Earth CrisisⓋ"]


def test_upsert_preserves_same_name_variants_with_different_details(test_db):
    first = db.upsert_dish(
        1, "Fries", "Side portion", "$4", db_path=test_db
    )
    second = db.upsert_dish(
        1, "FRIES", "Basket portion", "$6", db_path=test_db
    )

    assert second != first
    assert len(db.list_dishes(1, test_db)) == 2


def test_existing_duplicate_merge_preserves_classifications_and_reports(test_db):
    with db.connect(test_db) as conn:
        first = conn.execute(
            """INSERT INTO dishes (restaurant_id,name,raw_description,price,category)
               VALUES (1,'Earth CrisisⓋ','Red sauce','$18','food') RETURNING id"""
        ).fetchone()[0]
        second = conn.execute(
            """INSERT INTO dishes (restaurant_id,name,raw_description,price,category)
               VALUES (1,'EARTH CRISIS Ⓥ','Red sauce','$18','food') RETURNING id"""
        ).fetchone()[0]
        for dish_id in (first, second):
            conn.execute(
                """INSERT INTO classifications
                   (dish_id,verdict,confidence,reasoning,created_at)
                   VALUES (?,'vegan',0.9,'same dish','2026-07-05')""",
                (dish_id,),
            )
        conn.execute(
            """INSERT INTO reports
               (restaurant_id,dish_id,dish_name,issue_type,status,created_at)
               VALUES (1,?,'EARTH CRISIS Ⓥ','other','open','2026-07-05')""",
            (second,),
        )

    merged = db.deduplicate_dishes_for_restaurant(1, test_db)

    assert len(merged) == 1
    dishes = db.list_dishes(1, test_db)
    assert len(dishes) == 1
    assert dishes[0]["name"] == "Earth CrisisⓋ"
    with db.connect(test_db) as conn:
        assert conn.execute("SELECT count(*) FROM classifications").fetchone()[0] == 2
        report = conn.execute("SELECT dish_id,dish_name FROM reports").fetchone()
    assert report["dish_id"] == dishes[0]["id"]
    assert report["dish_name"] == "Earth CrisisⓋ"


def test_classification_result_dedupes_formatting_but_keeps_size_variant():
    def dish(name, price):
        return {
            "name": name,
            "description": "Red sauce and mushrooms",
            "price": price,
            "category": "food",
            "verdict": "vegan",
            "confidence": 0.9,
            "reasoning": "Plant-based",
            "evidence": "red sauce",
        }

    result = result_from_data(
        {
            "dishes": [
                dish("Earth CrisisⓋ", "$18"),
                dish("EARTH CRISIS Ⓥ", "$18"),
                dish("LARGE EARTH CRISISⓋ", "$28"),
            ]
        },
        provider="codex",
        model="m",
        billing="subscription",
    )

    assert result.ok
    assert [item.name for item in result.dishes] == [
        "Earth CrisisⓋ",
        "LARGE EARTH CRISISⓋ",
    ]
