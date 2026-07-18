"""Tests for per-page menu storage (db) and the automated quality audit.

Pins the storage semantics introduced with multi-page menus:
- one source row per kept page, combined on read with [page: url] headers
- re-scrapes prune pages that disappeared (and null classification links
  instead of violating the FK)
and the audit heuristics that replace manual deep dives.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import db  # noqa: E402
from menu_audit import audit_menus  # noqa: E402


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


REAL_MENU = "\n".join(
    ["Appetizers"]
    + [f"Dish {i} with grilled vegetables and tahini\n${i}.95" for i in range(1, 30)]
)


def test_menu_pages_stored_separately_and_combined(test_db):
    _add_restaurant(test_db, 1, "Multi Page Cafe", "https://x.com")
    db.replace_menu_texts(
        1,
        [("https://x.com/lunch", "lunch menu $9.99"),
         ("https://x.com/dinner", "dinner menu $19.99")],
        fetched_at="2026-07-03T00:00:00+00:00",
        db_path=test_db,
    )
    combined = db.get_menu_text(1, db_path=test_db)
    assert combined["page_count"] == 2
    assert "[page: https://x.com/lunch]" in combined["content"]
    assert "dinner menu $19.99" in combined["content"]


def test_rescrape_prunes_stale_pages_and_keeps_fk_intact(test_db):
    _add_restaurant(test_db, 1, "Moved Menu Cafe", "https://x.com")
    db.replace_menu_texts(
        1, [("https://x.com/old-menu", "old menu $1")],
        fetched_at="2026-01-01T00:00:00+00:00", db_path=test_db,
    )
    old_source = db.get_menu_text(1, db_path=test_db)
    dish_id = db.upsert_dish(1, "Old Dish", None, "$1", db_path=test_db)
    db.insert_classification(
        dish_id=dish_id, verdict="vegan", confidence=0.9, reasoning="r",
        source_id=old_source["id"], model_version="m",
        created_at="2026-01-01T00:00:00+00:00", db_path=test_db,
    )

    # Menu moved to a new URL; the old page must not linger.
    db.replace_menu_texts(
        1, [("https://x.com/new-menu", "new menu $2")],
        fetched_at="2026-07-03T00:00:00+00:00", db_path=test_db,
    )
    combined = db.get_menu_text(1, db_path=test_db)
    assert "old menu" not in combined["content"]
    assert combined["url"] == "https://x.com/new-menu"
    with db.connect(test_db) as conn:
        row = conn.execute(
            "SELECT source_id FROM classifications WHERE dish_id = ?", (dish_id,)
        ).fetchone()
    assert row["source_id"] is None  # nulled, not dangling


def test_audit_flags_tiny_and_priceless_menus(test_db):
    _add_restaurant(test_db, 1, "Marketing Only", "https://a.com")
    db.replace_menu_texts(
        1, [("https://a.com/", "Welcome! Great food, family owned.")],
        fetched_at="2026-07-03T00:00:00+00:00", db_path=test_db,
    )
    _add_restaurant(test_db, 2, "Healthy Menu", "https://b.com")
    db.replace_menu_texts(
        2, [("https://b.com/menu", REAL_MENU)],
        fetched_at="2026-07-03T00:00:00+00:00", db_path=test_db,
    )

    findings = {f["name"]: f["flags"] for f in audit_menus(test_db)}
    assert "Marketing Only" in findings
    assert any("suspiciously small" in fl for fl in findings["Marketing Only"])
    assert any("no prices" in fl for fl in findings["Marketing Only"])
    assert "Healthy Menu" not in findings


def test_audit_flags_small_classified_menus(test_db):
    # A full-looking menu that classified to only 3 dishes is the signature
    # of an incomplete capture that went live; a normally classified menu
    # (>= MIN_PLAUSIBLE_DISHES) and a never-classified one must not flag.
    def classify_dishes(rid: int, count: int) -> None:
        source = db.get_menu_text(rid, db_path=test_db)
        for i in range(count):
            dish_id = db.upsert_dish(
                rid, f"Dish {i}", None, "$9.95", db_path=test_db
            )
            db.insert_classification(
                dish_id=dish_id, verdict="vegan", confidence=0.9,
                reasoning="r", source_id=source["id"], model_version="m",
                created_at="2026-07-03T00:00:00+00:00", db_path=test_db,
            )

    for rid, name in (
        (1, "Three Dish Cafe"), (2, "Fully Classified"), (3, "Not Yet Run")
    ):
        _add_restaurant(test_db, rid, name, f"https://r{rid}.example")
        # Unique text per restaurant so the duplicate-menu check stays quiet.
        db.replace_menu_texts(
            rid, [(f"https://r{rid}.example/menu", f"{name}\n{REAL_MENU}")],
            fetched_at="2026-07-03T00:00:00+00:00", db_path=test_db,
        )
    classify_dishes(1, 3)
    classify_dishes(2, 12)

    findings = {f["name"]: f["flags"] for f in audit_menus(test_db)}
    assert any(
        "only 3 dishes extracted" in fl for fl in findings["Three Dish Cafe"]
    )
    assert "Fully Classified" not in findings
    assert "Not Yet Run" not in findings


def test_audit_flags_unresolved_dynamic_menu_loader(test_db):
    _add_restaurant(test_db, 1, "Dynamic Menu Cafe", "https://dynamic.example")
    db.replace_menu_texts(
        1,
        [(
            "https://dynamic.example/",
            "View our menus\nLoading Menu\nHappy Hour $5.00\n"
            + "Tacos, sliders, cocktails, brunch and desserts\n" * 40,
        )],
        fetched_at="2026-07-11T00:00:00+00:00",
        db_path=test_db,
    )

    finding = next(item for item in audit_menus(test_db) if item["restaurant_id"] == 1)

    assert any("unresolved dynamic menu loader" in flag for flag in finding["flags"])


def test_quality_review_persists_but_reopens_when_menu_changes(test_db):
    _add_restaurant(test_db, 1, "Reviewed Cafe", "https://reviewed.example")
    db.replace_menu_texts(
        1,
        [("https://reviewed.example/menu", "Tiny menu\nSoup\nSalad")],
        fetched_at="2026-07-05T00:00:00+00:00",
        db_path=test_db,
    )
    finding = next(item for item in audit_menus(test_db) if item["restaurant_id"] == 1)
    db.set_menu_quality_review(
        1,
        fingerprint=finding["fingerprint"],
        status="verified",
        db_path=test_db,
    )

    reviewed = next(item for item in audit_menus(test_db) if item["restaurant_id"] == 1)
    assert reviewed["review_status"] == "verified"

    # A later crawl changes the evidence, so the old human decision must not
    # silently hide the new warning.
    db.replace_menu_texts(
        1,
        [("https://reviewed.example/menu", "Different tiny menu\nPasta\nRice")],
        fetched_at="2026-07-06T00:00:00+00:00",
        db_path=test_db,
    )
    changed = next(item for item in audit_menus(test_db) if item["restaurant_id"] == 1)
    assert changed["fingerprint"] != finding["fingerprint"]
    assert changed["review_status"] is None


def test_audit_flags_duplicate_menus_across_restaurants(test_db):
    # Two locations storing byte-identical text = a generic platform page
    # (the 7-Eleven case), not either restaurant's menu.
    for rid, name in [(1, "Gas Mart Deli"), (2, "Corner Bodega")]:
        _add_restaurant(test_db, rid, name, f"https://chain{rid}.com")
        db.replace_menu_texts(
            rid, [(f"https://chain.com/menu?loc={rid}"[:30], REAL_MENU)],
            fetched_at="2026-07-03T00:00:00+00:00", db_path=test_db,
        )
    findings = {f["name"]: f["flags"] for f in audit_menus(test_db)}
    assert any(
        "identical menu text" in fl
        for flags in findings.values()
        for fl in flags
    )


def test_audit_flags_website_without_menu(test_db):
    _add_restaurant(test_db, 1, "Unscraped Diner", "https://x.com")
    findings = {f["name"]: f["flags"] for f in audit_menus(test_db)}
    assert any("no menu scraped" in fl for fl in findings["Unscraped Diner"])


def test_same_brand_locations_may_share_identical_menus(test_db):
    # Two First Watch locations share one national menu — that's correct,
    # not a copy-paste signal. Unrelated venues with identical text still
    # flag (the platform-boilerplate case).
    for rid, name in (
        (1, "First Watch"), (2, "First Watch"),
        (3, "Tainos Longwood"), (4, "Tainos Bakery & Deli (Casselberry)"),
        (5, "Corner Diner"),
    ):
        _add_restaurant(test_db, rid, name, f"https://r{rid}.example")
    shared = f"Shared Brand Menu\n{REAL_MENU}"
    for rid in (1, 2):
        db.replace_menu_texts(rid, [(f"https://r{rid}.example/menu", shared)],
                              fetched_at="2026-07-18T00:00:00+00:00", db_path=test_db)
    tainos = f"Tainos Menu\n{REAL_MENU}"
    for rid in (3, 4):
        db.replace_menu_texts(rid, [(f"https://r{rid}.example/menu", tainos)],
                              fetched_at="2026-07-18T00:00:00+00:00", db_path=test_db)
    # Corner Diner and Lakeside Grill share text: platform boilerplate.
    boilerplate = f"Boilerplate Platform Menu\n{REAL_MENU}"
    _add_restaurant(test_db, 6, "Lakeside Grill", "https://r6.example")
    for rid in (5, 6):
        db.replace_menu_texts(rid, [(f"https://r{rid}.example/menu", boilerplate)],
                              fetched_at="2026-07-18T00:00:00+00:00", db_path=test_db)

    by_id = {f["restaurant_id"]: f["flags"] for f in audit_menus(test_db)}
    dup = lambda rid: any("identical menu text" in fl for fl in by_id.get(rid, []))
    assert not dup(1) and not dup(2)  # First Watch pair: same brand
    assert not dup(3) and not dup(4)  # Tainos family: same brand
    assert dup(5) or dup(6)           # unrelated venues still flag
