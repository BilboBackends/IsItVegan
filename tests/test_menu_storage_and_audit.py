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


def test_audit_flags_duplicate_menus_across_restaurants(test_db):
    # Two locations storing byte-identical text = a generic platform page
    # (the 7-Eleven case), not either restaurant's menu.
    for rid, name in [(1, "Chain Store A"), (2, "Chain Store B")]:
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
