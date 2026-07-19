"""Ingest wires the photo-menu fallback in when the text path is exhausted."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import ingest  # noqa: E402
import photo_menu  # noqa: E402
from photo_menu import PhotoMenuResult  # noqa: E402
from scraper import ScrapeResult  # noqa: E402


def _seed(tmp_path, monkeypatch, website="https://wall.example"):
    path = str(tmp_path / "fallback.db")
    monkeypatch.setattr(
        db, "settings", dataclasses.replace(db.settings, database_path=path)
    )
    db.init_db()
    db.upsert_restaurants(
        [
            {
                "place_id": "p1",
                "name": "Walled Garden",
                "address": "1 Wall St",
                "website_url": website,
                "lat": 28.5,
                "lng": -81.4,
                "primary_type": "restaurant",
                "last_scraped_at": "t",
            }
        ]
    )
    return db.list_restaurants()[0]["id"]


def test_failed_scrape_falls_back_to_photo_menu(tmp_path, monkeypatch):
    rid = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        ingest,
        "scrape_menu_text",
        lambda url, **kw: ScrapeResult(url=url, ok=False, error="bot wall"),
    )
    photo_calls = []

    def fake_photo(restaurant_id, **kw):
        photo_calls.append(restaurant_id)
        return PhotoMenuResult(
            ok=True, tier="ocr", pages=[("img.jpg", "Falafel ... $9")],
            menu_score=0.9, char_count=400, cost_estimate=0.002, images_seen=2,
        )

    monkeypatch.setattr(photo_menu, "run", fake_photo)
    summary = ingest.run(restaurant_id=rid)
    assert photo_calls == [rid]
    assert summary["succeeded"] == 1
    assert summary["failed"] == 0
    # A photo success is not a crawl failure — no failure state recorded.
    profile = db.get_crawl_profile(rid)
    assert not (profile and profile.get("last_error"))


def test_photo_failure_records_combined_error(tmp_path, monkeypatch):
    rid = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        ingest,
        "scrape_menu_text",
        lambda url, **kw: ScrapeResult(url=url, ok=False, error="bot wall"),
    )
    monkeypatch.setattr(
        photo_menu,
        "run",
        lambda restaurant_id, **kw: PhotoMenuResult(
            ok=False, error="No menu-looking images found.", images_seen=0
        ),
    )
    summary = ingest.run(restaurant_id=rid)
    assert summary["succeeded"] == 0
    assert summary["failed"] == 1
    name, error = summary["failures"][0]
    assert "bot wall" in error and "photo fallback" in error
    profile = db.get_crawl_profile(rid)
    assert "photo fallback" in (profile.get("last_error") or "")


def test_social_profile_websites_skip_the_photo_fallback(tmp_path, monkeypatch):
    # The fallback is for real websites serving the menu as an image — an
    # Instagram-profile "website" has no menu image behind it, so it stays a
    # plain failure with no photo attempt (and no photo/vision spend).
    rid = _seed(tmp_path, monkeypatch, website="https://instagram.com/x")
    monkeypatch.setattr(
        ingest,
        "scrape_menu_text",
        lambda url, **kw: ScrapeResult(
            url=url, ok=False,
            error="Website is a social profile (instagram.com) — no menu to "
            "scrape. Photo fallback candidate.",
        ),
    )

    def must_not_run(restaurant_id, **kw):
        raise AssertionError("photo fallback must not run for social profiles")

    monkeypatch.setattr(photo_menu, "run", must_not_run)
    summary = ingest.run(restaurant_id=rid)
    assert summary["succeeded"] == 0
    assert summary["failed"] == 1
