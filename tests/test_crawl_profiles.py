"""Learned crawl routes persist, are reused, and safely fall back."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import ingest  # noqa: E402
import scraper  # noqa: E402


MENU_TEXT = "\n".join(
    ["Dinner Menu", "Entrees"]
    + [f"Plate {i} with tofu vegetables and rice ${i}.95" for i in range(1, 30)]
)


def _restaurant(path: str) -> int:
    db.upsert_restaurants(
        [{"name": "Learning Cafe", "place_id": "learn-1", "website_url": "https://x.com"}],
        path,
    )
    return db.list_restaurants(path)[0]["id"]


def test_crawl_profile_keeps_last_good_route_across_failure(tmp_path):
    path = str(tmp_path / "profiles.db")
    db.init_db(path)
    restaurant_id = _restaurant(path)

    db.record_crawl_success(
        restaurant_id,
        menu_urls=["https://x.com/menu", "https://x.com/menu#structured-menu"],
        crawl_method="http",
        content_hash="abc123",
        menu_score=0.92,
        char_count=4200,
        crawled_at="2026-07-05T10:00:00+00:00",
        db_path=path,
    )
    db.record_crawl_failure(
        restaurant_id,
        "temporary timeout",
        attempted_at="2026-07-06T10:00:00+00:00",
        db_path=path,
    )

    profile = db.get_crawl_profile(restaurant_id, path)
    assert profile["menu_urls"] == [
        "https://x.com/menu",
        "https://x.com/menu#structured-menu",
    ]
    assert profile["crawl_method"] == "http"
    assert profile["content_hash"] == "abc123"
    assert profile["consecutive_failures"] == 1
    assert profile["last_error"] == "temporary timeout"


def test_scraper_uses_valid_learned_route_before_discovery(monkeypatch):
    monkeypatch.setattr(
        scraper,
        "_collect_known_http",
        lambda urls, timeout: [(urls[0], MENU_TEXT)],
    )

    def unexpected_discovery(*args, **kwargs):
        raise AssertionError("full discovery should not run for a valid learned route")

    monkeypatch.setattr(scraper, "_collect_http", unexpected_discovery)
    result = scraper.scrape_menu_text(
        "https://learning-cafe.example",
        use_headless=False,
        crawl_context={
            "crawl_method": "http",
            "menu_urls": ["https://learning-cafe.example/menu"],
        },
    )

    assert result.ok
    assert result.used_learned_context is True
    assert result.crawl_method == "http"
    assert result.content_hash


def test_stale_learned_route_falls_back_to_discovery(monkeypatch):
    monkeypatch.setattr(scraper, "_collect_known_http", lambda urls, timeout: [])
    monkeypatch.setattr(
        scraper,
        "_collect_http",
        lambda url, timeout: (
            [("https://learning-cafe.example/new-menu", MENU_TEXT)],
            [],
            [],
            scraper._Fetched(html="<html></html>", status_code=200),
        ),
    )

    result = scraper.scrape_menu_text(
        "https://learning-cafe.example",
        use_headless=False,
        crawl_context={
            "crawl_method": "http",
            "menu_urls": ["https://learning-cafe.example/old-menu"],
        },
    )

    assert result.ok
    assert result.used_learned_context is False
    assert result.menu_url == "https://learning-cafe.example/new-menu"


def test_ingest_passes_and_refreshes_learned_context(monkeypatch):
    profile = {
        "crawl_method": "http",
        "menu_urls": ["https://x.com/menu"],
        "content_hash": "old",
    }
    captured = {}
    monkeypatch.setattr(ingest.db, "init_db", lambda: None)
    monkeypatch.setattr(
        ingest,
        "_targets",
        lambda *args, **kwargs: [
            {"id": 7, "name": "Learning Cafe", "website_url": "https://x.com"}
        ],
    )
    monkeypatch.setattr(ingest.db, "get_crawl_profile", lambda rid: profile)
    monkeypatch.setattr(ingest.db, "replace_menu_texts", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ingest.db,
        "record_crawl_success",
        lambda rid, **kwargs: captured.update({"restaurant_id": rid, **kwargs}),
    )

    def fake_scrape(url, *, crawl_context):
        assert crawl_context is profile
        return scraper.ScrapeResult(
            url="https://x.com/menu",
            ok=True,
            text=MENU_TEXT,
            char_count=len(MENU_TEXT),
            menu_url="https://x.com/menu",
            pages=[("https://x.com/menu", MENU_TEXT)],
            menu_score=0.9,
            is_menu=True,
            crawl_method="http",
            used_learned_context=True,
            content_hash="new",
        )

    monkeypatch.setattr(ingest, "scrape_menu_text", fake_scrape)
    summary = ingest.run()

    assert summary["succeeded"] == 1
    assert captured["restaurant_id"] == 7
    assert captured["menu_urls"] == ["https://x.com/menu"]
    assert captured["content_hash"] == "new"
