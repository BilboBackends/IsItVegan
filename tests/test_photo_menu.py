"""Tests for the photo-menu fallback (menu images -> Claude transcription).

Network-free: image discovery is exercised on fixture HTML, and the pipeline
test stubs fetching/downloading/transcription to pin the gating and the
persistence contract (photo menus must land exactly like text scrapes so
classification and versioning need no special handling).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import db  # noqa: E402
import photo_menu  # noqa: E402


@pytest.fixture()
def test_db(tmp_path):
    path = str(tmp_path / "test.db")
    db.init_db(path)
    return path


def _add_restaurant(path: str, rid: int, name: str, website: str | None) -> None:
    with db.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO restaurants (id, name, address, place_id, website_url)
            VALUES (?, ?, 'addr', ?, ?)
            """,
            (rid, name, f"place_{rid}", website),
        )


# The Neighbors Orlando shape: a real menu image, a logo mentioning the
# restaurant, and Square's nav hamburger literally named menu.svg.
NEIGHBORS_HTML = """
<html><body>
  <img src="/uploads/abc/food-menu-2026.jpg" alt="The Neighbors Orlando Food Menu">
  <img src="/uploads/abc/brand.png" alt="The Neighbors Orlando logo">
  <img src="/static/icons/sets/square/menu.svg" alt="">
  <img data-src="/uploads/abc/drink-menu.png" alt="Drinks">
  <img src="/uploads/abc/interior.jpg" alt="Dining room">
</body></html>
"""


def test_finds_menu_images_and_skips_logos_and_icons():
    urls = photo_menu.find_menu_image_urls(
        "https://example.com/food-menu", NEIGHBORS_HTML
    )
    # The alt-matched menu image and the src-matched lazy-loaded drink menu
    # qualify; the logo (anti-signal), nav icon (anti-signal), and interior
    # shot (no signal) do not.
    assert urls == [
        "https://example.com/uploads/abc/food-menu-2026.jpg",
        "https://example.com/uploads/abc/drink-menu.png",
    ]


def test_relative_urls_resolve_against_the_final_page_url():
    html = '<img src="../img/menu.jpg" alt="menu">'
    urls = photo_menu.find_menu_image_urls("https://example.com/pages/food", html)
    assert urls == ["https://example.com/img/menu.jpg"]


MENU_TEXT = "\n".join(
    ["Small Plates"]
    + [f"Dish {i} with chickpeas and tahini  ${i}.95" for i in range(1, 25)]
)


def _stub_pipeline(monkeypatch, transcription: photo_menu.Transcription):
    class FakeResponse:
        status_code = 200
        url = "https://example.com/food-menu"
        text = NEIGHBORS_HTML

    monkeypatch.setattr(photo_menu, "_fetch", lambda url: FakeResponse())
    monkeypatch.setattr(
        photo_menu, "_download_image", lambda url: (b"x" * 40_000, "image/jpeg")
    )
    monkeypatch.setattr(
        photo_menu, "read_menu_image", lambda *a, **k: transcription
    )


def test_ladder_prefers_ocr_when_text_scores_like_a_menu(monkeypatch):
    monkeypatch.setattr(
        photo_menu, "ocr_menu_image",
        lambda image_bytes: photo_menu.Transcription(
            ok=True, text=MENU_TEXT, cost_estimate=0.0015, method="ocr"
        ),
    )
    def boom(*a, **k):  # Claude must not be called for a clean OCR read
        raise AssertionError("escalated despite good OCR")
    monkeypatch.setattr(photo_menu, "transcribe_menu_image", boom)
    result = photo_menu.read_menu_image(b"img", "image/jpeg")
    assert result.method == "ocr" and result.is_menu
    assert result.cost_estimate == 0.0015


def test_detached_price_columns_escalate_but_inline_prices_do_not():
    # OCR split a two-column layout: dish block, then a run of bare prices.
    detached = MENU_TEXT + "\n8\n9\n12\n10"
    assert photo_menu._price_column_detached(detached)
    # Inline and alternating dish/price layouts stay on the cheap tier.
    assert not photo_menu._price_column_detached(MENU_TEXT)
    alternating = "Falafel Wrap\n$9\nLentil Soup\n$7\nKale Salad\n$11"
    assert not photo_menu._price_column_detached(alternating)


def test_ladder_escalates_to_claude_when_ocr_is_garbled_or_missing(monkeypatch):
    claude = photo_menu.Transcription(
        ok=True, is_menu=True, text=MENU_TEXT, cost_estimate=0.05
    )
    monkeypatch.setattr(
        photo_menu, "transcribe_menu_image", lambda *a, **k: claude
    )
    # Garbled OCR: plenty of characters, no prices/dish structure.
    monkeypatch.setattr(
        photo_menu, "ocr_menu_image",
        lambda image_bytes: photo_menu.Transcription(
            ok=True, text="lorem ipsum " * 40, cost_estimate=0.0015, method="ocr"
        ),
    )
    result = photo_menu.read_menu_image(b"img", "image/jpeg")
    assert result.method == "claude"
    assert result.cost_estimate == pytest.approx(0.0515)  # failed OCR still billed

    # No OCR key at all: straight to Claude, no OCR charge.
    claude.cost_estimate = 0.05
    monkeypatch.setattr(
        photo_menu, "ocr_menu_image",
        lambda image_bytes: photo_menu.Transcription(
            ok=False, error="No Google Vision API key", method="ocr"
        ),
    )
    result = photo_menu.read_menu_image(b"img", "image/jpeg")
    assert result.method == "claude"
    assert result.cost_estimate == pytest.approx(0.05)


def test_run_persists_like_a_text_scrape(test_db, monkeypatch):
    _add_restaurant(test_db, 1, "Photo Cafe", "https://example.com")
    _stub_pipeline(
        monkeypatch,
        photo_menu.Transcription(ok=True, is_menu=True, text=MENU_TEXT,
                                 cost_estimate=0.05),
    )
    result = photo_menu.run(1, db_path=test_db)
    assert result.ok
    assert len(result.pages) == 2  # both candidate images transcribed

    source = db.get_menu_text(1, db_path=test_db)
    assert "chickpeas and tahini" in source["content"]
    profile = db.get_crawl_profile(1, db_path=test_db)
    assert profile["crawl_method"] == "photo"
    assert profile["char_count"] == result.char_count
    versions = db.list_menu_versions(1, db_path=test_db)
    assert len(versions) == 1


def test_run_rejects_non_menu_and_tiny_transcriptions(test_db, monkeypatch):
    _add_restaurant(test_db, 1, "Photo Cafe", "https://example.com")
    for transcription in (
        photo_menu.Transcription(ok=True, is_menu=False, text=MENU_TEXT),
        photo_menu.Transcription(ok=True, is_menu=True, text="Beer $5"),
        photo_menu.Transcription(ok=False, error="boom"),
    ):
        _stub_pipeline(monkeypatch, transcription)
        result = photo_menu.run(1, db_path=test_db)
        assert not result.ok
    assert db.get_menu_text(1, db_path=test_db) is None
    assert db.get_crawl_profile(1, db_path=test_db) is None


def test_dry_run_stores_nothing(test_db, monkeypatch):
    _add_restaurant(test_db, 1, "Photo Cafe", "https://example.com")
    _stub_pipeline(
        monkeypatch,
        photo_menu.Transcription(ok=True, is_menu=True, text=MENU_TEXT),
    )
    result = photo_menu.run(1, dry_run=True, db_path=test_db)
    assert result.ok and result.char_count > 0
    assert db.get_menu_text(1, db_path=test_db) is None
