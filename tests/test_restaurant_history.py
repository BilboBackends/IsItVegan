"""Closure-check history: counted once, with a denominator, and it stays put."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import api  # noqa: E402
import db  # noqa: E402
import enrich  # noqa: E402
import restaurant_history  # noqa: E402
from location_filter import metro_from_area  # noqa: E402
from test_enrich import (  # noqa: E402, F401  (fixtures)
    _days_ago,
    _details,
    _http_error,
    _status,
    google,
    seeded,
)


def _changes(kind: str | None = None) -> list[dict]:
    return [
        c for c in db.list_restaurant_changes()
        if kind is None or c["change_type"] == kind
    ]


def _only_month() -> dict:
    (entry,) = restaurant_history.report()["months"]
    return entry


def _vegan_dish(restaurant_id: int, name: str) -> None:
    dish_id = db.upsert_dish(restaurant_id, name, None, "$12")
    db.insert_classification(
        dish_id=dish_id, verdict="vegan", confidence=0.95, reasoning="plants",
        source_id=None, model_version="test", created_at=_days_ago(1),
    )


def test_every_finding_is_kept_with_before_and_after(seeded, google):  # noqa: F811
    ids = seeded({k: _days_ago(90) for k in
                  ("shut", "moved", "paused", "back", "new", "edit", "far", "same")})
    db.update_place_status(
        ids["back"], checked_at=_days_ago(80), business_status="CLOSED_TEMPORARILY"
    )
    google.status.update({
        "shut": _status(business_status="CLOSED_PERMANENTLY"),
        "moved": _status(business_status="CLOSED_PERMANENTLY", moved_place_id="pX"),
        "paused": _status(business_status="CLOSED_TEMPORARILY"),
        "new": _status(name="Bluefin Sushi"),
        "edit": _status(name="Cafe edit & Bakery"),
        "far": _status(address="9 New St", lat=28.6),
    })
    enrich.refresh(do_all=True)
    found = {
        c["change_type"]: (c["restaurant_name"], c["old_value"], c["new_value"])
        for c in _changes()
    }
    assert found == {
        "closed": ("Cafe shut", "OPERATIONAL", None),
        "moved_listing": ("Cafe moved", None, "pX"),
        "temporarily_closed": ("Cafe paused", None, None),
        "reopened": ("Cafe back", None, None),
        "name_review": ("Cafe new", "Cafe new", "Bluefin Sushi"),
        "renamed": ("Cafe edit", "Cafe edit", "Cafe edit & Bakery"),
        "relocated": ("Cafe far", "A", "9 New St"),
    }


def test_standing_issues_count_once(seeded, google):  # noqa: F811
    seeded({"flagged": _days_ago(90), "dead": _days_ago(90)})
    google.status["flagged"] = _status(name="Bluefin Sushi")
    google.errors["dead"] = _http_error(404)
    for _ in range(3):
        enrich.refresh(do_all=True)
    assert len(_changes("name_review")) == 1
    assert len(_changes("missing")) == 1
    # A different new name is news.
    google.status["flagged"] = _status(name="Golden Noodle")
    enrich.refresh(do_all=True)
    assert len(_changes("name_review")) == 2


def test_disappearing_again_after_being_found_counts_again(seeded, google):  # noqa: F811
    seeded({"dead": _days_ago(90)})
    google.errors["dead"] = _http_error(404)
    enrich.refresh(do_all=True)
    google.errors.clear()
    enrich.refresh(do_all=True)
    google.errors["dead"] = _http_error(404)
    enrich.refresh(do_all=True)
    assert len(_changes("missing")) == 2


@pytest.mark.parametrize("status", [400, 503])
def test_only_unrecognized_listings_count_as_missing(seeded, google, status):  # noqa: F811
    seeded({"a": _days_ago(90)})
    google.errors["a"] = _http_error(status, "bad request")
    enrich.refresh(do_all=True)
    assert _changes() == []


def test_dish_counts_are_a_snapshot_that_outlives_the_restaurant(
    seeded, google  # noqa: F811
):
    ids = seeded({"a": _days_ago(90)})
    _vegan_dish(ids["a"], "Vegan Bowl")
    _vegan_dish(ids["a"], "Vegan Curry")
    google.status["a"] = _status(business_status="CLOSED_PERMANENTLY")
    enrich.refresh(do_all=True)
    db.delete_dishes_for_restaurant(ids["a"])
    db.delete_restaurant(ids["a"], expected_name="Cafe a")
    (change,) = _changes("closed")
    assert change["restaurant_id"] is None
    assert change["restaurant_name"] == "Cafe a"
    assert (change["dishes"], change["vegan_meals"]) == (2, 2)
    entry = _only_month()
    assert entry["counts"] == {"closed": 1}
    assert entry["vegan_meals_lost"] == 2
    assert entry["closure_rate"] == 100.0


def test_resumed_checks_add_up_to_one_month(seeded, google):  # noqa: F811
    seeded({f"p{i}": _days_ago(100 - i) for i in range(8)})
    google.status["p1"] = _status(business_status="CLOSED_PERMANENTLY")
    google.status["p5"] = _status(
        business_status="CLOSED_PERMANENTLY", moved_place_id="pX"
    )
    google.errors["p6"] = _http_error(404)
    enrich.refresh(stale_days=28, limit=3)
    enrich.refresh(stale_days=28)
    assert len(db.list_status_checks()) == 2
    entry = _only_month()
    assert (entry["checked"], entry["failed"]) == (7, 1)
    assert entry["counts"] == {"closed": 1, "moved_listing": 1, "missing": 1}
    assert entry["closure_rate"] == 28.6  # both leave the site: 2 of 7


def test_interrupted_check_keeps_its_numbers(seeded, google):  # noqa: F811
    seeded({"a": _days_ago(90), "b": _days_ago(80), "c": _days_ago(70)})
    google.status["a"] = _status(business_status="CLOSED_PERMANENTLY")
    google.errors["b"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        enrich.refresh(do_all=True)
    entry = _only_month()
    assert (entry["checked"], entry["counts"]) == (1, {"closed": 1})


def test_stopped_check_says_why(seeded, google):  # noqa: F811
    seeded({"a": _days_ago(90)})
    google.errors["a"] = _http_error(403)
    enrich.refresh(do_all=True)
    (check,) = db.list_status_checks()
    assert check["checked"] == 0 and "403" in check["stopped"]
    assert _only_month()["closure_rate"] is None  # nothing to divide by


def test_rates_split_by_metro(seeded, google):  # noqa: F811
    ids = seeded({k: _days_ago(90) for k in ("o1", "o2", "c1")})
    for key, city in (("o1", "Orlando"), ("o2", "Maitland"), ("c1", "Cape Coral")):
        address = f"1 Main St, {city}, FL 32801, USA"
        db.update_place_status(ids[key], checked_at=_days_ago(60), address=address)
        google.status[key] = _status(address=address)
    google.status["o2"]["business_status"] = "CLOSED_PERMANENTLY"
    enrich.refresh(do_all=True)
    metros = {m["metro"]: (m["closed"], m["checked"], m["rate"])
              for m in _only_month()["metros"]}
    assert metros == {
        metro_from_area("Orlando"): (1, 2, 50.0),
        metro_from_area("Cape Coral"): (0, 1, 0.0),
    }
    assert _changes("closed")[0]["area"] == "Maitland"


def test_dry_runs_and_empty_runs_leave_no_trace(seeded, google):  # noqa: F811
    seeded({"a": _days_ago(90)})
    google.status["a"] = _status(business_status="CLOSED_PERMANENTLY")
    enrich.refresh(do_all=True, dry_run=True)
    assert db.list_status_checks() == [] and _changes() == []
    google.status.clear()
    enrich.refresh(stale_days=28)
    enrich.refresh(stale_days=28)  # nothing due
    assert len(db.list_status_checks()) == 1


def test_closures_found_by_full_enrichment_are_kept_too(seeded, google):  # noqa: F811
    ids = seeded({"a": _days_ago(90)})
    google.details["a"] = _details(business_status="CLOSED_PERMANENTLY")
    enrich.run(do_all=True)
    enrich.run(restaurant_id=ids["a"])  # already known to be closed
    (change,) = _changes("closed")
    assert change["check_id"] is None


def test_endpoint_reports_the_history(seeded, google):  # noqa: F811
    client = api.app.test_client()
    seeded({"a": _days_ago(90)})
    assert client.get("/api/restaurant-changes").get_json() == {"months": []}
    google.status["a"] = _status(business_status="CLOSED_PERMANENTLY")
    enrich.refresh(do_all=True)
    (entry,) = client.get("/api/restaurant-changes").get_json()["months"]
    assert entry["counts"] == {"closed": 1}
    assert entry["changes"][0]["restaurant_name"] == "Cafe a"
