"""A re-scrape that loses a menu's dishes must not replace the stored menu."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import api  # noqa: E402
import db  # noqa: E402
import ingest  # noqa: E402
import menu_guard  # noqa: E402
import photo_menu  # noqa: E402
from photo_menu import PhotoMenuResult  # noqa: E402
from scraper import ScrapeResult  # noqa: E402

DISHES = [
    ("Falafel Bowl", "crispy chickpea falafel over rice with tahini"),
    ("Lentil Soup", "slow simmered red lentils finished with lemon"),
    ("Hummus Plate", "house ground chickpeas with warm pita triangles"),
    ("Tofu Banh Mi", "pressed tofu with pickled carrot and cilantro"),
    ("Garden Salad", "local greens tossed in sherry vinaigrette dressing"),
    ("Chicken Shawarma", "marinated thighs shaved from the rotating spit"),
    ("Beef Kofta", "ground beef skewers charred over open flame"),
    ("Baba Ganoush", "smoked eggplant whipped with garlic and olive oil"),
    ("Stuffed Grape Leaves", "vine leaves rolled around herbed jasmine rice"),
    ("Mushroom Risotto", "arborio rice stirred with roasted wild mushrooms"),
]
NAMES = [name for name, _ in DISHES]


def menu(dishes, price="12.00"):
    # Long enough that the stored menu isn't taken for a partial capture.
    return "\n".join(
        f"{name} ${price}\n{text}. Served daily from our kitchen in generous "
        f"portions, {name.lower()} is prepared fresh for every single guest."
        for name, text in dishes
    )


OLD = menu(DISHES)
OTHER = [
    (f"Seasonal Plate {word}", f"entirely new {word} cooking with {word} sauce")
    for word in ("alpha", "bravo", "cedar", "delta", "ember", "fjord", "grove", "haven")
]


def test_same_menu_is_accepted_however_it_is_laid_out():
    assert menu_guard.hold_reason(OLD, OLD, NAMES) is None
    reflowed = " | ".join(OLD.upper().split("\n"))
    assert menu_guard.hold_reason(OLD, reflowed, NAMES) is None
    assert menu_guard.hold_reason(OLD, menu(DISHES, price="14.50"), NAMES) is None


def test_accents_and_punctuation_do_not_count_as_lost_dishes():
    old = menu([("Jalapeño Café—Bowl", "x"), *DISHES])
    new = menu([("Jalapeno Cafe Bowl", "x"), *DISHES])
    names = ["Jalapeño Café—Bowl", *NAMES]
    assert menu_guard.hold_reason(old, new, names) is None
    # Names outside the Latin alphabet are compared too, not skipped.
    words = ["麻婆豆腐", "宫保鸡丁", "担担面", "小笼包", "红烧肉"]
    old = "\n".join(f"{w} {t}" for w, (_, t) in zip(words, DISHES)) * 12
    assert menu_guard.hold_reason(old, old, words) is None
    gone = menu_guard.hold_reason(old, menu(OTHER) * 2, words)
    assert gone and "0 of 5 known dishes" in gone


def test_a_mostly_different_page_is_held():
    reason = menu_guard.hold_reason(OLD, menu(DISHES[:3] + OTHER), NAMES)
    assert reason.startswith(menu_guard.HELD_PREFIX)
    assert "mostly different" in reason
    assert "3 of 10 known dishes" in reason
    assert "Missing, for example: baba ganoush" in reason


def test_a_much_shorter_page_is_held():
    reason = menu_guard.hold_reason(OLD, menu(DISHES[:5]), NAMES)
    assert "much shorter" in reason


def test_a_list_of_names_without_the_descriptions_is_held():
    # All the dishes, none of the evidence the verdicts rest on.
    reason = menu_guard.hold_reason(OLD, "\n".join(NAMES), NAMES)
    assert "much shorter" in reason
    assert "10 of 10 known dishes" in reason


def test_losing_dishes_with_nothing_new_is_held():
    reason = menu_guard.hold_reason(OLD, menu(DISHES[:7]), NAMES)
    assert "adds nothing new" in reason


def test_a_changed_menu_that_replaces_its_lost_dishes_is_accepted():
    assert menu_guard.hold_reason(OLD, menu(DISHES[:7] + OTHER[:4]), NAMES) is None


def test_a_few_lost_dishes_are_accepted():
    assert menu_guard.hold_reason(OLD, menu(DISHES[:9]), NAMES) is None


def test_a_fuller_capture_replaces_a_partial_one():
    partial = "\n".join(NAMES)
    assert len(partial) < 1_500
    assert menu_guard.hold_reason(partial, menu(OTHER) * 2, NAMES) is None


def test_too_few_recognizable_dishes_to_judge():
    assert menu_guard.hold_reason(OLD, "closed", NAMES[:4]) is None
    # Reworded dish names the old page never showed say nothing about the new.
    assert menu_guard.hold_reason(OLD, "closed", [f"{n} Deluxe" for n in NAMES]) is None


# --- wired into ingestion ---------------------------------------------------


@pytest.fixture()
def stored(tmp_path, monkeypatch):
    path = str(tmp_path / "guard.db")
    monkeypatch.setattr(
        db, "settings", dataclasses.replace(db.settings, database_path=path)
    )
    db.init_db()
    db.upsert_restaurants([{
        "place_id": "p1", "name": "Cedar Grill", "address": "1 Main St",
        "website_url": "https://cedar.example", "lat": 28.5, "lng": -81.4,
        "primary_type": "restaurant", "last_scraped_at": "t",
    }])
    rid = db.list_restaurants()[0]["id"]
    db.replace_menu_texts(
        rid, [("https://cedar.example/menu", OLD)], fetched_at="2026-07-01T00:00:00+00:00"
    )
    for name in NAMES:
        db.upsert_dish(rid, name, None, "$12.00", "food")
    db.record_crawl_success(
        rid, menu_urls=["https://cedar.example/menu"], crawl_method="http",
        content_hash="old", menu_score=0.9, char_count=len(OLD),
    )
    return rid


@pytest.fixture()
def no_photos(monkeypatch):
    def run(*_args, **_kw):
        raise AssertionError("a held menu must not spend money on photos")

    monkeypatch.setattr(photo_menu, "run", run)


def scrape_returns(monkeypatch, text):
    monkeypatch.setattr(ingest, "scrape_menu_text", lambda url, **kw: ScrapeResult(
        url=url, ok=True, text=text, pages=[("https://cedar.example/home", text)],
        menu_score=0.8, char_count=len(text), crawl_method="http", content_hash="new",
    ))


def test_a_bad_re_scrape_keeps_the_stored_menu(stored, no_photos, monkeypatch):
    scrape_returns(monkeypatch, menu(OTHER))
    events = []
    summary = ingest.run(restaurant_id=stored, on_progress=events.append)
    assert summary["succeeded"] == 0 and summary["failed"] == 1
    assert "mostly different" in summary["failures"][0][1]
    assert db.get_menu_text(stored)["content"] == OLD
    assert db.list_menu_versions(stored) == []
    # The good route is still what the next scrape tries first.
    profile = db.get_crawl_profile(stored)
    assert profile["menu_urls"] == ["https://cedar.example/menu"]
    assert profile["content_hash"] == "old"
    assert profile["last_error"].startswith(menu_guard.HELD_PREFIX)
    assert [e["result"]["held"] for e in events if "result" in e] == [True]


def test_accept_changes_takes_the_new_menu_and_clears_the_hold(stored, monkeypatch):
    scrape_returns(monkeypatch, menu(OTHER))
    ingest.run(restaurant_id=stored)
    summary = ingest.run(restaurant_id=stored, accept_changes=True)
    assert summary["succeeded"] == 1
    assert db.get_menu_text(stored)["content"] == menu(OTHER)
    assert db.get_crawl_profile(stored)["last_error"] is None


def test_a_good_re_scrape_is_stored_as_before(stored, monkeypatch):
    scrape_returns(monkeypatch, menu(DISHES, price="13.00"))
    assert ingest.run(restaurant_id=stored)["succeeded"] == 1
    assert "13.00" in db.get_menu_text(stored)["content"]


def test_dry_run_reports_the_hold_without_writing(stored, monkeypatch):
    scrape_returns(monkeypatch, menu(OTHER))
    assert ingest.run(restaurant_id=stored, dry_run=True)["failed"] == 1
    assert db.get_crawl_profile(stored)["last_error"] is None


def test_a_first_menu_has_nothing_to_be_guarded_against(stored, monkeypatch):
    with db.connect() as conn:
        conn.execute("DELETE FROM sources")
    scrape_returns(monkeypatch, menu(OTHER))
    assert ingest.run(restaurant_id=stored)["succeeded"] == 1


def test_photo_fallback_is_told_whether_changes_are_accepted(stored, monkeypatch):
    monkeypatch.setattr(ingest, "scrape_menu_text", lambda url, **kw: ScrapeResult(
        url=url, ok=False, error="bot wall",
    ))
    calls = []

    def fake_photo(restaurant_id, **kw):
        calls.append(kw)
        return PhotoMenuResult(ok=False, error="none")

    monkeypatch.setattr(photo_menu, "run", fake_photo)
    ingest.run(restaurant_id=stored)
    ingest.run(restaurant_id=stored, accept_changes=True)
    assert calls == [{"accept_changes": False}, {"accept_changes": True}]


def test_a_bad_photo_menu_keeps_the_stored_menu(stored, monkeypatch):
    text = menu(OTHER)
    monkeypatch.setattr(photo_menu, "_candidate_page_urls", lambda *a, **k: [])
    monkeypatch.setattr(
        photo_menu, "_places_photo_candidates",
        lambda r: [("photo:1", b"x", "image/jpeg")],
    )
    monkeypatch.setattr(
        photo_menu, "read_menu_image",
        lambda *a: photo_menu.Transcription(
            ok=True, is_menu=True, text=text, method="ocr"
        ),
    )
    held = photo_menu.run(stored)
    assert not held.ok and held.error.startswith(menu_guard.HELD_PREFIX)
    assert db.get_menu_text(stored)["content"] == OLD
    assert photo_menu.run(stored, accept_changes=True).ok
    assert db.get_menu_text(stored)["content"] == text


def test_admin_can_see_and_accept_a_held_menu(stored, monkeypatch):
    scrape_returns(monkeypatch, menu(OTHER))
    client = api.app.test_client()
    first = client.post("/api/ingest", json={"restaurant_id": stored}).get_json()
    assert first["failed"] == 1
    listed = client.get("/api/scrape-failures").get_json()["failures"]
    assert [(f["id"], f["held"]) for f in listed] == [(stored, True)]
    # Only an explicit true counts, so a stray value can't replace a menu.
    client.post("/api/ingest", json={"restaurant_id": stored, "accept_changes": "no"})
    assert db.get_menu_text(stored)["content"] == OLD
    second = client.post(
        "/api/ingest", json={"restaurant_id": stored, "accept_changes": True}
    ).get_json()
    assert second["succeeded"] == 1
    assert client.get("/api/scrape-failures").get_json()["failures"] == []


def test_an_ordinary_failure_is_not_marked_held(stored):
    db.record_crawl_failure(stored, "bot wall")
    listed = api.app.test_client().get("/api/scrape-failures").get_json()["failures"]
    assert [f["held"] for f in listed] == [False]
