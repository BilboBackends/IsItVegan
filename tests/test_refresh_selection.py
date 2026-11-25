"""Regression tests for Admin refresh toggles and selected batch targets."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import classify  # noqa: E402
import db  # noqa: E402
import ingest  # noqa: E402


def _row(rid: int, *, enabled: int = 1, primary_type: str = "restaurant") -> dict:
    return {
        "id": rid,
        "name": f"Restaurant {rid}",
        "website_url": f"https://example.com/{rid}",
        "refresh_enabled": enabled,
        "consumer_hidden": 0,
        "primary_type": primary_type,
        "has_menu_text": 1,
    }


def test_refresh_enabled_persists(tmp_path):
    path = str(tmp_path / "refresh.db")
    db.init_db(path)
    db.upsert_restaurants(
        [{"name": "Cafe", "place_id": "p1", "website_url": "https://x.com"}],
        path,
    )
    restaurant = db.list_restaurants(path)[0]
    assert restaurant["refresh_enabled"] == 1
    assert db.set_restaurant_refresh_enabled(restaurant["id"], False, path)
    assert db.list_restaurants(path)[0]["refresh_enabled"] == 0


def test_restaurant_list_includes_latest_classification_time(tmp_path):
    path = str(tmp_path / "classified.db")
    db.init_db(path)
    db.upsert_restaurants(
        [{"name": "Cafe", "place_id": "p1", "website_url": "https://x.com"}],
        path,
    )
    restaurant = db.list_restaurants(path)[0]
    dish_id = db.upsert_dish(
        restaurant["id"], "Vegan Bowl", None, "$12", db_path=path
    )
    db.insert_classification(
        dish_id=dish_id,
        verdict="vegan",
        confidence=0.95,
        reasoning="Plant ingredients",
        source_id=None,
        model_version="test",
        created_at="2026-07-03T14:30:00+00:00",
        db_path=path,
    )

    assert db.list_restaurants(path)[0]["last_classified_at"] == "2026-07-03T14:30:00+00:00"


def test_selected_ingest_skips_paused_and_non_consumer(monkeypatch):
    rows = [
        _row(1),
        _row(2, enabled=0),
        _row(3, primary_type="convenience_store"),
    ]
    monkeypatch.setattr(ingest.db, "list_restaurants", lambda: rows)
    targets = ingest._targets(None, False, restaurant_ids=[1, 2, 3])
    assert [target["id"] for target in targets] == [1]


def test_selected_classify_skips_paused_but_explicit_row_bypasses(monkeypatch):
    rows = [_row(1), _row(2, enabled=0)]
    monkeypatch.setattr(classify.db, "list_restaurants", lambda: rows)
    monkeypatch.setattr(classify.db, "restaurants_needing_classification", lambda: [1, 2])

    assert [row["id"] for row in classify._targets(None, False, [1, 2])] == [1]
    # The per-row reclassify action remains a deliberate override.
    assert [row["id"] for row in classify._targets(2, False)] == [2]


def test_classify_can_stop_cleanly_between_restaurants(monkeypatch):
    rows = [_row(1), _row(2)]
    monkeypatch.setattr(classify.db, "init_db", lambda: None)
    monkeypatch.setattr(classify, "_targets", lambda *args: rows)
    monkeypatch.setattr(
        classify.db,
        "get_menu_text",
        lambda restaurant_id: {"id": restaurant_id, "content": "Vegan bowl $12"},
    )
    monkeypatch.setattr(classify.db, "snapshot_dishes", lambda restaurant_id: {})
    stop_checks = iter([False, True])

    result = classify.run(
        dry_run=True,
        mock=True,
        should_stop=lambda: next(stop_checks),
    )

    assert result["cancelled"] is True
    assert result["ok"] == 1


def test_archiving_turns_refresh_off_and_restoring_turns_it_on(tmp_path):
    path = str(tmp_path / "archive.db")
    db.init_db(path)
    db.upsert_restaurants(
        [{"name": "Cafe", "place_id": "p1", "website_url": "https://x.com"}], path
    )
    rid = db.list_restaurants(path)[0]["id"]
    db.set_restaurant_archived(rid, True, path)
    assert db.list_restaurants(path)[0]["refresh_enabled"] == 0
    db.set_restaurant_archived(rid, False, path)
    assert db.list_restaurants(path)[0]["refresh_enabled"] == 1


def test_archive_endpoint_returns_the_new_refresh_state(tmp_path, monkeypatch):
    import dataclasses

    import api

    path = str(tmp_path / "archive_api.db")
    monkeypatch.setattr(
        db, "settings", dataclasses.replace(db.settings, database_path=path)
    )
    db.init_db()
    db.upsert_restaurants(
        [{"name": "Cafe", "place_id": "p1", "website_url": "https://x.com"}]
    )
    rid = db.list_restaurants()[0]["id"]
    client = api.app.test_client()
    for archived in (True, False):
        body = client.patch(
            f"/api/restaurants/{rid}/archived", json={"archived": archived}
        ).get_json()
        assert body["refresh_enabled"] == int(not archived)
        assert body["is_consumer_venue"] is (not archived)


def test_bulk_jobs_skip_archived_rows_whatever_the_switch_says(monkeypatch):
    live, gone = _row(1), {**_row(2), "archived": 1}  # refresh still enabled
    monkeypatch.setattr(db, "list_restaurants", lambda *a, **kw: [live, gone])
    assert [t["id"] for t in ingest._targets(None, True)] == [1]
    assert [t["id"] for t in ingest._targets(None, False, None, [1, 2])] == [1]
    assert [t["id"] for t in classify._targets(None, True)] == [1]
    assert [t["id"] for t in classify._targets(None, False, [1, 2])] == [1]
