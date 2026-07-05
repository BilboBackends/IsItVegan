"""Permanent restaurant deletion is complete, transactional, and confirmed."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402


def _restaurant(path: str, name: str, place_id: str) -> int:
    db.upsert_restaurants(
        [{"name": name, "place_id": place_id, "website_url": "https://example.com"}],
        path,
    )
    return next(row["id"] for row in db.list_restaurants(path) if row["place_id"] == place_id)


def test_delete_restaurant_removes_every_dependent_record(tmp_path):
    path = str(tmp_path / "delete.db")
    db.init_db(path)
    restaurant_id = _restaurant(path, "Delete Me Cafe", "delete-me")
    survivor_id = _restaurant(path, "Keep Me Cafe", "keep-me")
    timestamp = "2026-07-05T12:00:00+00:00"

    db.upsert_menu_text(restaurant_id, "https://example.com/menu", "Menu $12", timestamp, path)
    with db.connect(path) as conn:
        source_id = conn.execute(
            "SELECT id FROM sources WHERE restaurant_id = ?", (restaurant_id,)
        ).fetchone()[0]
    dish_id = db.upsert_dish(
        restaurant_id, "Vegan Bowl", "Tofu and rice", "$12", db_path=path
    )
    db.insert_classification(
        dish_id=dish_id,
        verdict="vegan",
        confidence=0.95,
        reasoning="Plant ingredients",
        source_id=source_id,
        model_version="test",
        created_at=timestamp,
        db_path=path,
    )
    db.create_report(restaurant_id, "other", dish_id=dish_id, note="test", db_path=path)
    db.record_crawl_success(
        restaurant_id,
        menu_urls=["https://example.com/menu"],
        crawl_method="http",
        content_hash="hash-1",
        menu_score=0.9,
        char_count=100,
        db_path=path,
    )
    db.record_menu_version(restaurant_id, "Menu $12", "hash-1", db_path=path)
    db.record_dish_changes(
        restaurant_id,
        [{"change_type": "added", "dish_name": "Vegan Bowl", "new_price": "$12"}],
        db_path=path,
    )
    db.set_menu_quality_review(
        restaurant_id, fingerprint="warning-1", status="known_issue", db_path=path
    )

    with pytest.raises(ValueError, match="does not match"):
        db.delete_restaurant(
            restaurant_id, expected_name="Wrong Name", db_path=path
        )
    assert any(row["id"] == restaurant_id for row in db.list_restaurants(path))

    deleted = db.delete_restaurant(
        restaurant_id, expected_name="Delete Me Cafe", db_path=path
    )
    assert deleted["dishes"] == 1
    assert deleted["classifications"] == 1
    assert deleted["reports"] == 1
    assert deleted["menu_versions"] == 1
    assert db.delete_restaurant(
        restaurant_id, expected_name="Delete Me Cafe", db_path=path
    ) is None

    with db.connect(path) as conn:
        for table in (
            "restaurants",
            "dishes",
            "sources",
            "classifications",
            "reports",
            "crawl_profiles",
            "menu_versions",
            "dish_changes",
            "menu_quality_reviews",
        ):
            column = "id" if table == "restaurants" else "restaurant_id"
            if table == "classifications":
                count = conn.execute(
                    "SELECT COUNT(*) FROM classifications WHERE dish_id = ?", (dish_id,)
                ).fetchone()[0]
            elif table == "sources":
                count = conn.execute(
                    "SELECT COUNT(*) FROM sources WHERE restaurant_id = ? OR dish_id = ?",
                    (restaurant_id, dish_id),
                ).fetchone()[0]
            else:
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (restaurant_id,)
                ).fetchone()[0]
            assert count == 0, table

    assert any(row["id"] == survivor_id for row in db.list_restaurants(path))
