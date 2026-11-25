"""Google calls cost money per row: bounded, resumable, crash-tolerant.

Two jobs share one fetch loop: enrich.run (full details, for new rows) and
enrich.refresh (essentials, for the recurring check).
"""
from __future__ import annotations

import dataclasses
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402

import api  # noqa: E402
import db  # noqa: E402
import enrich  # noqa: E402
import places_client as pc  # noqa: E402


def _days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _details(**overrides) -> dict:
    return {
        "serves_vegetarian": True, "price_level": None,
        "primary_type": "restaurant", "editorial_summary": None, "rating": 4.5,
        "user_rating_count": 10, "open_now": True, "opening_hours": [],
        "business_status": "OPERATIONAL", "website_url": None, **overrides,
    }


def _status(**overrides) -> dict:
    """An essentials response that matches what seed() stores."""
    return {
        "name": None, "address": "A", "lat": 28.5, "lng": -81.4,
        "business_status": "OPERATIONAL", "moved_place_id": None, **overrides,
    }


def _http_error(status: int, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://places.googleapis.com/v1/places/x")
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request,
        response=httpx.Response(status, text=body, request=request),
    )


def _row(restaurant_id: int) -> dict:
    return next(r for r in db.list_restaurants() if r["id"] == restaurant_id)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """seeded({place_id: last enriched ISO time or None}) -> {place_id: id}"""
    path = str(tmp_path / "enrich.db")
    monkeypatch.setattr(
        db, "settings", dataclasses.replace(db.settings, database_path=path)
    )
    fake_key = dataclasses.replace(enrich.settings, google_places_api_key="k")
    monkeypatch.setattr(enrich, "settings", fake_key)
    monkeypatch.setattr(api, "settings", fake_key)
    monkeypatch.setattr(enrich, "_REFRESH_CALLS_PER_SECOND", 1_000_000.0)

    def seed(enriched: dict[str, str | None]) -> dict[str, int]:
        db.init_db()
        db.upsert_restaurants(
            [
                {"place_id": pid, "name": f"Cafe {pid}", "address": "A",
                 "lat": 28.5, "lng": -81.4, "website_url": None,
                 "last_scraped_at": "t"}
                for pid in enriched
            ]
        )
        ids = {r["place_id"]: r["id"] for r in db.list_restaurants()}
        for pid, when in enriched.items():
            if when:
                db.update_food_signals(ids[pid], enriched_at=when, **_details())
        return ids

    return seed


class _Google:
    def __init__(self):
        self.calls: list[str] = []  # every paid call, either tier
        self.tiers: list[str] = []
        self.errors: dict[str, BaseException] = {}
        self.details: dict[str, dict] = {}
        self.status: dict[str, dict] = {}

    def _call(self, tier: str, place_id: str) -> None:
        self.calls.append(place_id)
        self.tiers.append(tier)
        if place_id in self.errors:
            raise self.errors[place_id]


@pytest.fixture
def google(monkeypatch):
    fake = _Google()

    def details(place_id, *, api_key):
        fake._call("full", place_id)
        return fake.details.get(place_id, _details())

    def status(place_id, *, api_key, client):
        fake._call("essentials", place_id)
        return fake.status.get(place_id, _status())

    monkeypatch.setattr(enrich, "fetch_place_details", details)
    monkeypatch.setattr(enrich, "fetch_place_status", status)
    return fake


@pytest.fixture(params=["run", "refresh"])
def job(request):
    return getattr(enrich, request.param)


# --- shared by both jobs ----------------------------------------------------


def test_dead_place_id_is_skipped_and_the_rest_still_refresh(seeded, google, job):
    ids = seeded({"a": _days_ago(90), "dead": _days_ago(80), "c": _days_ago(70)})
    google.errors["dead"] = _http_error(404)
    result = job(do_all=True)
    assert google.calls == ["a", "dead", "c"]
    assert result["fetched"] == 2
    assert [f["id"] for f in result["failed"]] == [ids["dead"]]
    assert result["stopped"] is None
    # A dead ID is reported, never archived: it may have moved, not closed.
    assert not _row(ids["dead"])["archived"]


def test_many_dead_ids_never_block_the_queue(seeded, google, job):
    # Unchecked rows sort first on every run, so a cluster of dead IDs must
    # not trip the outage stop.
    dead = {f"dead{i}": _days_ago(100 - i) for i in range(8)}
    seeded({**dead, "ok": _days_ago(50)})
    for i, pid in enumerate(dead):
        google.errors[pid] = _http_error(404 if i % 2 else 400, "invalid place id")
    result = job(stale_days=28)
    assert result["fetched"] == 1
    assert result["stopped"] is None


def test_bulk_runs_skip_archived(seeded, google, job):
    ids = seeded({"a": _days_ago(90), "gone": _days_ago(90), "new": None})
    db.set_restaurant_archived(ids["gone"], True)
    db.set_restaurant_archived(ids["new"], True)
    job(do_all=True)
    assert google.calls == ["a"]
    job(stale_days=28)
    assert "gone" not in google.calls and "new" not in google.calls


def test_limited_refresh_resumes_without_paying_twice(seeded, google, job):
    seeded({"a": _days_ago(90), "b": _days_ago(80), "c": _days_ago(70),
            "never": None, "fresh": _days_ago(2)})
    first = job(stale_days=28, limit=2)
    assert google.calls == ["never", "a"]  # longest-unchecked first
    assert first["remaining"] == 2
    second = job(stale_days=28, limit=2)
    assert google.calls == ["never", "a", "b", "c"]
    assert second["remaining"] == 0
    job(stale_days=28, limit=2)
    assert len(google.calls) == 4  # nothing due, nothing billed


def test_interrupted_run_keeps_finished_rows(seeded, google, job):
    seeded({"a": _days_ago(90), "b": _days_ago(80), "c": _days_ago(70)})
    google.errors["b"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        job(stale_days=28)
    google.errors.clear()
    google.calls.clear()
    job(stale_days=28)
    assert google.calls == ["b", "c"]


@pytest.mark.parametrize(
    "error",
    [_http_error(401), _http_error(403), _http_error(429),
     _http_error(400, '{"message": "API key not valid."}')],
)
def test_account_level_errors_stop_immediately(seeded, google, job, error):
    seeded({"a": _days_ago(90), "b": _days_ago(80), "c": _days_ago(70)})
    google.errors["b"] = error
    result = job(do_all=True)
    assert google.calls == ["a", "b"]
    assert result["fetched"] == 1
    assert result["stopped"]
    assert result["remaining"] == 1


def test_outage_stops_after_consecutive_failures(seeded, google, job):
    pids = {f"p{i}": _days_ago(100 - i) for i in range(10)}
    seeded(pids)
    for pid in pids:
        google.errors[pid] = httpx.ConnectTimeout("timed out")
    result = job(do_all=True)
    assert len(google.calls) == enrich._MAX_CONSECUTIVE_OUTAGES
    assert result["stopped"]
    assert result["remaining"] == 10 - enrich._MAX_CONSECUTIVE_OUTAGES


def test_scattered_outages_do_not_stop_the_run(seeded, google, job):
    pids = {f"p{i}": _days_ago(100 - i) for i in range(12)}
    seeded(pids)
    for i, pid in enumerate(pids):
        if i % 2:
            google.errors[pid] = _http_error(503)
    result = job(do_all=True)
    assert len(google.calls) == 12
    assert result["fetched"] == 6
    assert result["stopped"] is None


@pytest.mark.parametrize(
    "name, writer", [("run", "update_food_signals"), ("refresh", "update_place_status")]
)
def test_database_errors_still_end_the_run(seeded, google, monkeypatch, name, writer):
    # Carrying on would keep buying data that can't be saved.
    seeded({"a": _days_ago(90), "b": _days_ago(80)})

    def locked(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(db, writer, locked)
    with pytest.raises(RuntimeError):
        getattr(enrich, name)(do_all=True)
    assert google.calls == ["a"]


@pytest.mark.parametrize("bad", [{"limit": 0}, {"limit": -1}, {"stale_days": 0}])
def test_invalid_bounds_never_reach_google(seeded, google, job, bad):
    seeded({"a": _days_ago(90), "b": _days_ago(80)})
    with pytest.raises(ValueError):
        job(do_all=True, **bad)
    assert google.calls == []


def test_dry_run_writes_nothing(seeded, google, job):
    ids = seeded({"a": _days_ago(90)})
    before = _row(ids["a"])
    google.details["a"] = _details(business_status="CLOSED_PERMANENTLY")
    google.status["a"] = _status(business_status="CLOSED_PERMANENTLY", name="New")
    job(do_all=True, dry_run=True)
    assert _row(ids["a"]) == before


# --- full enrichment --------------------------------------------------------


def test_full_enrichment_targets_new_rows_and_explicit_ids(seeded, google):
    ids = seeded({"old": _days_ago(90), "new": None, "gone": _days_ago(90)})
    db.set_restaurant_archived(ids["gone"], True)
    enrich.run()
    assert google.calls == ["new"]
    enrich.run(restaurant_id=ids["gone"])  # explicit IDs still reach archived
    assert google.calls == ["new", "gone"]
    assert set(google.tiers) == {"full"}


def test_full_enrichment_still_archives_permanent_closures(seeded, google):
    ids = seeded({"a": _days_ago(90)})
    google.details["a"] = _details(business_status="CLOSED_PERMANENTLY")
    enrich.run(do_all=True)
    assert _row(ids["a"])["archived"]


# --- essentials refresh -----------------------------------------------------


def test_status_mask_stays_pro_tier():
    # Any Enterprise field bills the whole call at $20+/1k with only 1k free.
    pro_or_cheaper = {
        "id", "displayName", "formattedAddress", "location", "primaryType",
        "businessStatus", "movedPlaceId",
    }
    assert set(pc.STATUS_FIELD_MASK.split(",")) == pro_or_cheaper


def test_status_fetch_requests_only_that_mask_and_leaves_gaps_as_none():
    seen = {}

    def handler(request):
        seen["mask"] = request.headers["X-Goog-FieldMask"]
        return httpx.Response(200, json={"id": "a", "businessStatus": "OPERATIONAL"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        found = pc.fetch_place_status("a", api_key="k", client=client)
    assert seen["mask"] == pc.STATUS_FIELD_MASK
    # Never the place ID standing in for a missing name: that reads as a rename.
    assert found == {
        "name": None, "address": None, "lat": None, "lng": None,
        "business_status": "OPERATIONAL", "moved_place_id": None,
    }


def test_refresh_uses_the_cheap_tier_only(seeded, google):
    seeded({"a": _days_ago(90), "new": None})
    enrich.refresh(do_all=True)
    assert set(google.tiers) == {"essentials"}


def test_refresh_never_replaces_full_enrichment(seeded, google):
    # A checked-but-never-enriched row still needs its full details.
    ids = seeded({"new": None, "old": _days_ago(90)})
    enrich.refresh(do_all=True)
    google.calls.clear()
    enrich.run()
    assert google.calls == ["new"]
    old = _row(ids["old"])
    assert old["rating"] == 4.5  # essentials leave the rest alone
    assert old["enriched_at"] < _days_ago(80)  # still dates the hours/ratings


def test_recent_full_enrichment_counts_as_a_check(seeded, google):
    seeded({"fresh": _days_ago(3), "old": _days_ago(40)})
    enrich.refresh(stale_days=28)
    assert google.calls == ["old"]


def test_closed_restaurant_is_archived_with_its_data(seeded, google):
    ids = seeded({"a": _days_ago(90), "b": _days_ago(80)})
    google.status["a"] = _status(business_status="CLOSED_PERMANENTLY")
    result = enrich.refresh(do_all=True)
    assert [r["id"] for r in result["closed"]] == [ids["a"]]
    row = _row(ids["a"])
    assert row["archived"] and row["business_status"] == "CLOSED_PERMANENTLY"
    assert row["name"] == "Cafe a"
    assert not _row(ids["b"])["archived"]


def test_relocated_business_is_reported_as_moved_not_closed(seeded, google):
    ids = seeded({"a": _days_ago(90)})
    google.status["a"] = _status(
        business_status="CLOSED_PERMANENTLY", moved_place_id="new-spot"
    )
    result = enrich.refresh(do_all=True)
    assert result["closed"] == []
    assert result["moved_listing"] == [
        {"id": ids["a"], "name": "Cafe a", "place_id": "new-spot"}
    ]
    assert _row(ids["a"])["archived"]  # the old address must stop showing


def test_temporary_closure_and_reopening_are_reported_once(seeded, google):
    ids = seeded({"a": _days_ago(90)})
    google.status["a"] = _status(business_status="CLOSED_TEMPORARILY")
    assert len(enrich.refresh(do_all=True)["temporarily_closed"]) == 1
    assert not _row(ids["a"])["archived"]
    assert enrich.refresh(do_all=True)["temporarily_closed"] == []
    google.status["a"] = _status()
    assert len(enrich.refresh(do_all=True)["reopened"]) == 1
    assert enrich.refresh(do_all=True)["reopened"] == []


def test_missing_fields_keep_stored_values(seeded, google):
    ids = seeded({"a": _days_ago(90)})
    before = _row(ids["a"])
    google.status["a"] = _status(
        address=None, lat=28.9, lng=None, business_status=None
    )
    enrich.refresh(do_all=True)
    after = _row(ids["a"])
    for key in ("name", "address", "lat", "lng", "business_status"):
        assert after[key] == before[key]


def test_possible_new_business_is_reported_until_handled(seeded, google):
    # Its stored dishes belong to the old restaurant, so never relabel them.
    ids = seeded({"a": _days_ago(90)})
    google.status["a"] = _status(name="Bluefin Sushi")
    for _ in range(2):
        result = enrich.refresh(do_all=True)
        assert result["renamed"] == [
            {"id": ids["a"], "name": "Cafe a", "new_name": "Bluefin Sushi",
             "review": True}
        ]
    assert _row(ids["a"])["name"] == "Cafe a"


def test_similar_rename_is_saved_and_reported_once(seeded, google):
    ids = seeded({"a": _days_ago(90)})
    google.status["a"] = _status(name="Cafe a & Bakery")
    result = enrich.refresh(do_all=True)
    assert result["renamed"][0]["review"] is False
    assert _row(ids["a"])["name"] == "Cafe a & Bakery"
    assert enrich.refresh(do_all=True)["renamed"] == []


ADDRESS = "1 N Orange Ave, Orlando, FL 32801, USA"


@pytest.mark.parametrize(
    "old, new",
    [  # the first four are from the first real run, 2026-11-25
        ("Z Cafe Restaurant", "Ngọc Hà Vietnamese Restaurant"),
        ("Wo Sushi", "Wa sushi"),
        ("The Boheme", "Libra Restaurant"),
        ("Bread Pete", "Mel's Sunrise Cafe & Bakery"),
        ("Orlando Thai Cafe", "Orlando Sushi House"),  # place and type only
        ("Thai Cafe", "Thai House"),
    ],
)
def test_names_sharing_only_generic_words_need_review(old, new):
    assert not enrich._resembles(old, new, ADDRESS)
    assert not enrich._resembles(new, old, ADDRESS)


@pytest.mark.parametrize(
    "old, new",
    [
        ("Cuban’s Coco Loco", "Coco Loco Latin Grill"),
        ("Jalapenos Mexican Eats", "Jalapeños Mexican Eats"),
        ("Grain and Berry - Maitland", "Grain & Berry - Maitland"),
        ("Koa Sushi & Poke II Mills 50", "Koa Sushi & Poke Mills 50"),
        ("Lelulo's Pizza Life", "Lelulo's Pizzeria - Orlando"),
        ("Curry Element", "CURRY ELEMENT ZABIHA HALAL INDO-PAK CHINESE CUISINE"),
        ("GATEWAY TO INDIA [BAR & GRILL][AWARDED INDIAN CUISINE]",
         "GATEWAY TO INDIA ORLANDO"),
        ("Whole Foods Bakery", "Whole Foods Bakery Orlando"),
        ("Pizza Kitchen", "Pizza Kitchen & Bar"),
    ],
)
def test_lightly_edited_names_are_the_same_business(old, new):
    assert enrich._resembles(old, new, ADDRESS)


def test_punctuation_only_name_changes_are_not_reported(seeded, google):
    ids = seeded({"a": _days_ago(90)})
    google.status["a"] = _status(name="CAFE - A")
    assert enrich.refresh(do_all=True)["renamed"] == []
    assert _row(ids["a"])["name"] == "Cafe a"


def test_address_moves_are_saved_and_pin_nudges_are_quiet(seeded, google):
    ids = seeded({"far": _days_ago(90), "near": _days_ago(80)})
    google.status["far"] = _status(address="9 New St", lat=28.6)  # ~11 km
    google.status["near"] = _status(lat=28.5005)  # ~55 m
    result = enrich.refresh(do_all=True)
    assert [r["id"] for r in result["relocated"]] == [ids["far"]]
    row = _row(ids["far"])
    assert (row["address"], row["lat"]) == ("9 New St", 28.6)
    assert enrich.refresh(do_all=True)["relocated"] == []


def test_refresh_needs_an_explicit_scope(seeded, google):
    seeded({"a": _days_ago(90)})
    with pytest.raises(ValueError):
        enrich.refresh()
    assert google.calls == []


# --- entry points -----------------------------------------------------------


@pytest.mark.parametrize(
    "body, tier, calls",
    [
        ({"stale_days": 28, "limit": 1}, "essentials", ["a"]),
        ({"all": True}, "essentials", ["new", "a", "b", "fresh"]),
        ({"stale_days": 28, "limit": 1, "full": True}, "full", ["new"]),
        ({}, "full", ["new"]),
    ],
)
def test_endpoint_defaults_bulk_work_to_the_cheap_tier(
    seeded, google, body, tier, calls
):
    ids = seeded({"a": _days_ago(90), "b": _days_ago(80), "fresh": _days_ago(1),
                  "new": None})
    if body == {"stale_days": 28, "limit": 1}:
        db.update_place_status(ids["new"], checked_at=_days_ago(1))
    response = api.app.test_client().post("/api/enrich", json=body)
    assert response.status_code == 200
    assert google.calls == calls
    assert set(google.tiers) == {tier}


@pytest.mark.parametrize("bad", ["1000", 0, -5, True, 1.5])
def test_endpoint_rejects_malformed_bounds(seeded, google, bad):
    seeded({"a": _days_ago(90)})
    response = api.app.test_client().post(
        "/api/enrich", json={"all": True, "limit": bad}
    )
    assert response.status_code == 400
    assert google.calls == []


@pytest.mark.parametrize(
    "argv, tier",
    [
        (["--stale-days", "28"], "essentials"),
        (["--all"], "essentials"),
        (["--all", "--full"], "full"),
        (["--stale-days", "28", "--full"], "full"),
        ([], "full"),
        (["--limit", "5"], "full"),
    ],
)
def test_command_line_defaults_bulk_work_to_the_cheap_tier(
    seeded, google, monkeypatch, argv, tier
):
    seeded({"a": _days_ago(90), "new": None})
    monkeypatch.setattr(sys, "argv", ["enrich.py", *argv])
    enrich.main()
    assert set(google.tiers) == {tier}
