"""The Admin methods census: how each live menu was acquired, by cost tier."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api  # noqa: E402
import db  # noqa: E402


def _restaurant(path, rid, name, website="https://x.example"):
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO restaurants (id, name, address, place_id, website_url)"
            " VALUES (?, ?, 'addr', ?, ?)",
            (rid, name, f"place_{rid}", website),
        )


def _menu(path, rid, method, tier=None):
    db.replace_menu_texts(
        rid, [(f"https://x.example/{rid}", "Falafel $9\nHummus $8")],
        fetched_at="2026-07-19T00:00:00+00:00", db_path=path,
    )
    db.record_crawl_success(
        rid, menu_urls=[f"https://x.example/{rid}"], crawl_method=method,
        content_hash=f"h{rid}", menu_score=0.9, char_count=100,
        photo_tier=tier, db_path=path,
    )


def test_pipeline_metrics_buckets_by_method_and_photo_tier(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    db.init_db(path)
    _restaurant(path, 1, "Http Cafe");      _menu(path, 1, "http")
    _restaurant(path, 2, "Headless Cafe");  _menu(path, 2, "headless")
    _restaurant(path, 3, "OCR Cafe");       _menu(path, 3, "photo", "ocr")
    _restaurant(path, 4, "Haiku Cafe");     _menu(path, 4, "photo", "haiku")
    _restaurant(path, 5, "Opus Cafe");      _menu(path, 5, "photo", "opus")
    _restaurant(path, 6, "Old Photo Cafe"); _menu(path, 6, "photo", None)
    # Failures: a social profile, a plain failure, one never attempted,
    # and one with no website at all.
    _restaurant(path, 7, "Facebook Bar")
    db.record_crawl_failure(7, "Website is a social profile (facebook.com)",
                            db_path=path)
    _restaurant(path, 8, "Broken Site")
    db.record_crawl_failure(8, "No real menu found (score 0.30)", db_path=path)
    _restaurant(path, 9, "Fresh Row")
    _restaurant(path, 10, "No Website", website=None)

    real_connect = db.connect
    monkeypatch.setattr(api.db, "connect",
                        lambda db_path=None: real_connect(path))
    monkeypatch.setattr(api.db, "init_db", lambda *a, **k: None)
    data = api.app.test_client().get("/api/pipeline-metrics").get_json()

    assert data["acquired"] == {
        "http": 1, "headless": 1, "photo_ocr": 1, "photo_haiku": 1,
        "photo_opus": 1, "photo_untiered": 1, "other": 0,
    }
    assert data["unscraped"] == {
        "social_profile": 1, "failed": 1, "unattempted": 1, "no_website": 1,
    }
    assert data["total_with_menu"] == 6
    assert data["total_active"] == 10
