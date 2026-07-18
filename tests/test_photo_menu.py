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
    # Named matches lead; the interior shot rides along only because the
    # page itself is a menu path; the logo and nav icon never qualify.
    assert urls == [
        "https://example.com/uploads/abc/food-menu-2026.jpg",
        "https://example.com/uploads/abc/drink-menu.png",
        "https://example.com/uploads/abc/interior.jpg",
    ]


def test_menu_page_context_admits_unnamed_images():
    # Tacos Los Campeones: the /menu page serves its menu photo under a UUID
    # filename with empty alt. On a menu-path page every non-anti-signal
    # image qualifies; named matches still order first.
    html = """
    <img src="/uploads/2e7f0e52-7d64.jpeg" alt="">
    <img src="/uploads/food-menu.jpg" alt="menu">
    <img src="/uploads/logo.png" alt="logo">
    """
    urls = photo_menu.find_menu_image_urls("https://tacos.example/menu", html)
    assert urls == [
        "https://tacos.example/uploads/food-menu.jpg",
        "https://tacos.example/uploads/2e7f0e52-7d64.jpeg",
    ]
    # Same images on a non-menu page: only the named match qualifies.
    urls = photo_menu.find_menu_image_urls("https://tacos.example/", html)
    assert urls == ["https://tacos.example/uploads/food-menu.jpg"]


def test_menus_published_as_linked_image_files_are_found():
    # The Wellborn: menu JPEGs live behind <a href> links, with only logos
    # in <img> tags.
    html = """
    <img src="/assets/Wellborn+Logo.png" alt="logo">
    <a href="/s/Wellborn_2026_Q2_Web_Dinner.jpg">Dinner</a>
    <a href="/s/IMG_2292.jpeg">Specials</a>
    <a href="/menu.pdf">PDF</a>
    <a href="/visit">Visit</a>
    """
    urls = photo_menu.find_menu_image_urls("https://w.example/menu", html)
    assert urls == [
        "https://w.example/s/Wellborn_2026_Q2_Web_Dinner.jpg",
        "https://w.example/s/IMG_2292.jpeg",
    ]
    # Off a menu page, an anchor whose text/href says menu still qualifies.
    html2 = '<a href="/files/dinner-menu.jpg">Our Menu</a>'
    assert photo_menu.find_menu_image_urls("https://w.example/", html2) == [
        "https://w.example/files/dinner-menu.jpg"
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
    monkeypatch.setattr(photo_menu, "_places_photo_candidates", lambda r: [])


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


def _word(text: str, x: int, y: int, w: int = 40, h: int = 20, brk: str | None = None):
    symbols = [{"text": ch} for ch in text]
    if brk:
        symbols[-1]["property"] = {"detectedBreak": {"type": brk}}
    return {
        "symbols": symbols,
        "boundingBox": {"vertices": [
            {"x": x, "y": y}, {"x": x + w, "y": y},
            {"x": x + w, "y": y + h}, {"x": x, "y": y + h},
        ]},
    }


def _paragraph(words):
    return {"words": words}


def test_geometry_repair_reattaches_price_column_rows():
    # Two dishes at y=100 and y=140; their prices OCR'd as a separate
    # right-hand column block on the same rows.
    annotation = {
        "pages": [{"blocks": [
            {"paragraphs": [_paragraph([
                _word("Garlic", 10, 100), _word("Buns", 60, 100, brk="LINE_BREAK"),
                _word("Zucchini", 10, 140), _word("Hummus", 80, 140, brk="LINE_BREAK"),
            ])]},
            {"paragraphs": [_paragraph([
                _word("9", 400, 100, brk="LINE_BREAK"),
                _word("10", 400, 140, brk="LINE_BREAK"),
            ])]},
        ]}]
    }
    repaired = photo_menu._reattach_detached_prices(
        photo_menu._ocr_lines(annotation)
    )
    assert repaired == "Garlic Buns 9\nZucchini Hummus 10"


def test_geometry_repair_leaves_alternating_layouts_alone():
    # Price on its own row BELOW the dish (no vertical overlap): fail-open,
    # raw text stands.
    annotation = {
        "pages": [{"blocks": [{"paragraphs": [_paragraph([
            _word("Falafel", 10, 100, brk="LINE_BREAK"),
            _word("9", 10, 130, brk="LINE_BREAK"),
        ])]}]}]
    }
    repaired = photo_menu._reattach_detached_prices(
        photo_menu._ocr_lines(annotation)
    )
    assert repaired is None


def test_photos_with_no_ocr_text_never_reach_claude(monkeypatch):
    # A food/interior photo OCRs to almost nothing — that IS the verdict.
    # A sweep without this gate paid Haiku + Opus per glamour shot.
    def boom(*a, **k):
        raise AssertionError("Claude called for a text-less photo")
    monkeypatch.setattr(photo_menu, "transcribe_menu_image", boom)
    monkeypatch.setattr(
        photo_menu, "ocr_menu_image",
        lambda image_bytes: photo_menu.Transcription(
            ok=True, text="Grand Opening!", cost_estimate=0.0015, method="ocr"
        ),
    )
    result = photo_menu.read_menu_image(b"img", "image/jpeg")
    assert result.ok and not result.is_menu
    assert result.cost_estimate == 0.0015


def test_cheap_models_not_a_menu_verdict_is_final(monkeypatch):
    # Haiku confidently saying is_menu=false must not buy an Opus retry.
    calls = []

    def fake_transcribe(image_bytes, media_type, *, model=None):
        calls.append(model)
        return photo_menu.Transcription(
            ok=True, is_menu=False, text="", cost_estimate=0.01
        )

    monkeypatch.setattr(photo_menu, "transcribe_menu_image", fake_transcribe)
    monkeypatch.setattr(
        photo_menu, "ocr_menu_image",
        lambda image_bytes: photo_menu.Transcription(
            ok=True, text="storefront sign daily specials board " * 10,
            cost_estimate=0.0015, method="ocr",
        ),
    )
    result = photo_menu.read_menu_image(b"img", "image/jpeg")
    assert len(calls) == 1  # one cheap call, no escalation
    assert not result.is_menu


def test_claude_rung_escalates_haiku_misreads_to_opus(monkeypatch):
    calls = []

    def fake_transcribe(image_bytes, media_type, *, model=None):
        calls.append(model)
        if model == photo_menu._ESCALATION_MODEL:
            return photo_menu.Transcription(
                ok=True, is_menu=True, text=MENU_TEXT, cost_estimate=0.05
            )
        # Haiku read a stylized menu into confetti: fails the menu gates.
        return photo_menu.Transcription(
            ok=True, is_menu=True, text="???", cost_estimate=0.01
        )

    monkeypatch.setattr(photo_menu, "transcribe_menu_image", fake_transcribe)
    monkeypatch.setattr(
        photo_menu, "ocr_menu_image",
        lambda image_bytes: photo_menu.Transcription(
            ok=False, error="No Google Vision API key", method="ocr"
        ),
    )
    result = photo_menu.read_menu_image(b"img", "image/jpeg")
    assert calls == [photo_menu.settings.photo_menu_vision_model,
                     photo_menu._ESCALATION_MODEL]
    assert result.text == MENU_TEXT
    assert result.cost_estimate == pytest.approx(0.06)  # both rungs billed


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
    assert len(result.pages) == 3  # every candidate image transcribed

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


def test_places_photos_are_the_last_resort_image_source(test_db, monkeypatch):
    # Social-only/placeholder websites yield no web images; the restaurant's
    # Maps listing photos go through the same ladder and gates.
    _add_restaurant(test_db, 1, "Facebook Only BBQ", "https://nope.example")

    class EmptyPage:
        status_code = 200
        url = "https://nope.example/"
        text = "<html><body><p>Welcome!</p></body></html>"

    monkeypatch.setattr(photo_menu, "_fetch", lambda url: EmptyPage())
    monkeypatch.setattr(photo_menu, "_fetch_rendered", lambda url: (None, "x"))
    monkeypatch.setattr(
        photo_menu, "_places_photo_candidates",
        lambda r: [("google-places-photo:places/p1/photos/a", b"img", "image/jpeg")],
    )
    monkeypatch.setattr(
        photo_menu, "read_menu_image",
        lambda *a, **k: photo_menu.Transcription(
            ok=True, is_menu=True, text=MENU_TEXT, cost_estimate=0.002,
            method="ocr",
        ),
    )
    result = photo_menu.run(1, db_path=test_db)
    assert result.ok
    assert result.pages[0][0].startswith("google-places-photo:")
    profile = db.get_crawl_profile(1, db_path=test_db)
    assert profile["crawl_method"] == "photo"


def test_dry_run_stores_nothing(test_db, monkeypatch):
    _add_restaurant(test_db, 1, "Photo Cafe", "https://example.com")
    _stub_pipeline(
        monkeypatch,
        photo_menu.Transcription(ok=True, is_menu=True, text=MENU_TEXT),
    )
    result = photo_menu.run(1, dry_run=True, db_path=test_db)
    assert result.ok and result.char_count > 0
    assert db.get_menu_text(1, db_path=test_db) is None
