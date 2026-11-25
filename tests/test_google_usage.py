"""Free-call counting: right SKU, right month, right price, never in the way."""
from __future__ import annotations

import dataclasses
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402

import api  # noqa: E402
import db  # noqa: E402
import enrich  # noqa: E402
import google_usage as gu  # noqa: E402
import places_client as pc  # noqa: E402


def _used() -> dict[str, int]:
    return {s["id"]: s["used"] for s in gu.usage()["skus"] if s["used"]}


def _at(monkeypatch, *args) -> None:
    moment = datetime(*args, tzinfo=timezone.utc)
    monkeypatch.setattr(gu, "_utcnow", lambda: moment)


# --- counting ---------------------------------------------------------------


def test_prices_match_googles_list():
    listed = {s.id: (s.free, s.price_per_1000) for s in gu.SKUS.values()}
    assert listed == {
        "details_pro": (5_000, 17.0),
        "details_atmosphere": (1_000, 25.0),
        "nearby_pro": (5_000, 32.0),
        "text_pro": (5_000, 32.0),
        "photos": (1_000, 7.0),
    }


def test_counts_add_up_per_sku():
    for _ in range(3):
        gu.record(gu.DETAILS_PRO)
    gu.record(gu.DETAILS_ATMOSPHERE, 2)
    assert _used() == {"details_pro": 3, "details_atmosphere": 2}


def test_unknown_sku_is_a_programming_error():
    with pytest.raises(KeyError):
        gu.record("nope")


@pytest.mark.parametrize(
    "moment, month",
    [
        # Google resets at midnight Pacific, not UTC: 07:00 in daylight time,
        # 08:00 in standard time.
        ((2026, 12, 1, 7, 59), "2026-11"),
        ((2026, 12, 1, 8, 0), "2026-12"),
        ((2026, 7, 1, 6, 59), "2026-06"),
        ((2026, 7, 1, 7, 0), "2026-07"),
        ((2026, 11, 1, 7, 0), "2026-11"),  # still daylight time at midnight
        ((2026, 3, 1, 7, 59), "2026-02"),
        ((2027, 1, 1, 7, 59), "2026-12"),  # year rollover
        ((2027, 1, 1, 8, 0), "2027-01"),
    ],
)
def test_billing_month_follows_pacific_midnight(moment, month):
    name, starts, ends = gu._period(datetime(*moment, tzinfo=timezone.utc))
    assert name == month
    assert starts <= datetime(*moment, tzinfo=timezone.utc) < ends


def test_boundaries_match_the_time_zone_database():
    try:
        from zoneinfo import ZoneInfo

        pacific = ZoneInfo("America/Los_Angeles")
    except Exception:
        pytest.skip("no time zone database here")
    for year in range(2024, 2036):
        for month in range(1, 13):
            assert gu._boundary(year, month) == datetime(
                year, month, 1, tzinfo=pacific
            )


def test_a_new_month_starts_from_zero(monkeypatch):
    _at(monkeypatch, 2026, 11, 30, 12)
    gu.record(gu.DETAILS_PRO, 4_900)
    assert gu.price(gu.DETAILS_PRO, 1_000)["billable"] == 900
    _at(monkeypatch, 2026, 12, 1, 8)
    assert _used() == {}
    assert gu.price(gu.DETAILS_PRO, 1_000)["billable"] == 0


def test_partial_first_month_says_so_and_later_months_do_not(monkeypatch):
    _at(monkeypatch, 2026, 11, 25, 12)
    assert gu.usage()["counting_since"].startswith("2026-11-25")
    _at(monkeypatch, 2026, 12, 25, 12)
    assert gu.usage()["counting_since"] is None


def test_busy_database_never_breaks_a_call_or_loses_the_count(monkeypatch):
    gu.record(gu.DETAILS_PRO)
    real = db.connect

    def busy(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "connect", busy)
    gu.record(gu.DETAILS_PRO)  # must not raise
    monkeypatch.setattr(db, "connect", real)
    assert _used() == {"details_pro": 2}  # unsaved count is still visible
    gu.record(gu.DETAILS_PRO)
    assert gu._pending == {}
    assert _used() == {"details_pro": 3}


def test_set_count_overrides_this_month(monkeypatch):
    gu.record(gu.TEXT_PRO, 5)
    gu.set_count(gu.TEXT_PRO, 120)
    gu.record(gu.TEXT_PRO)
    assert _used() == {"text_pro": 121}
    for bad in (("nope", 1), (gu.TEXT_PRO, -1)):
        with pytest.raises(ValueError):
            gu.set_count(*bad)


# --- pricing ----------------------------------------------------------------


@pytest.mark.parametrize(
    "used, calls, billable, cost",
    [
        (0, 1_096, 0, 0.0),
        (3_904, 1_096, 0, 0.0),  # exactly uses it up
        (3_905, 1_096, 1, 0.02),
        (4_500, 1_096, 596, 10.13),
        (6_000, 1_096, 1_096, 18.63),  # already past it
    ],
)
def test_quote_charges_only_past_the_free_calls(used, calls, billable, cost):
    gu.set_count(gu.DETAILS_PRO, used)
    quote = gu.price(gu.DETAILS_PRO, calls)
    assert (quote["billable"], quote["cost"]) == (billable, cost)
    assert quote["tier"] == "Place Details Pro"
    assert quote["cost_without_free"] == 18.63


def test_usage_reports_overage():
    gu.set_count(gu.DETAILS_ATMOSPHERE, 1_096)
    row = next(s for s in gu.usage()["skus"] if s["id"] == gu.DETAILS_ATMOSPHERE)
    assert (row["remaining"], row["over"], row["spent"]) == (0, 96, 2.40)


def test_skus_have_separate_allowances():
    gu.set_count(gu.DETAILS_ATMOSPHERE, 1_000)
    assert gu.price(gu.DETAILS_PRO, 1_096)["cost"] == 0.0


# --- every paid call is recorded, on the SKU it bills ------------------------


@pytest.fixture
def http(monkeypatch):
    """Serve every httpx.Client from one canned response."""
    state = {"status": 200, "json": {"places": [], "id": "p1"}}

    def handler(request):
        if request.url.path.endswith("/media"):
            return httpx.Response(
                state["status"], content=b"x", headers={"content-type": "image/jpeg"}
            )
        return httpx.Response(state["status"], json=state["json"])

    real = httpx.Client

    def client(**kw):
        kw.pop("transport", None)
        return real(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(pc.httpx, "Client", client)
    return state


def _status_call():
    with pc.httpx.Client() as client:
        pc.fetch_place_status("p1", api_key="k", client=client)


CALLS = [
    (lambda: pc.fetch_place_details("p1", api_key="k"), "details_atmosphere"),
    (lambda: pc.fetch_place_summary("p1", api_key="k"), "details_pro"),
    (_status_call, "details_pro"),
    (lambda: pc.prospect_places("q", api_key="k"), "text_pro"),
    (lambda: pc.search_place_candidates("q", api_key="k"), "text_pro"),
    (lambda: pc.download_place_photo("places/p/photos/q", api_key="k"), "photos"),
]


@pytest.mark.parametrize("call, sku", CALLS)
def test_successful_calls_are_counted(http, call, sku):
    call()
    assert _used() == {sku: 1}


@pytest.mark.parametrize("call, sku", CALLS)
@pytest.mark.parametrize("status", [404, 429, 500])
def test_failed_calls_are_not_counted(http, call, sku, status):
    # Google bills "successful requests" only.
    http["status"] = status
    with pytest.raises(httpx.HTTPStatusError):
        call()
    assert _used() == {}


def test_nearby_search_is_counted(http):
    with pc.httpx.Client() as client:
        pc._search_circle(client, api_key="k", lat=1, lng=1, radius_meters=10)
    assert _used() == {"nearby_pro": 1}


def test_unlimited_free_calls_are_not_counted(http):
    with pc.httpx.Client() as client:
        pc.text_search_ids(client, "q", (0, 0, 1, 1), api_key="k")
    pc.fetch_place_photo_names("p1", api_key="k")
    assert _used() == {}


# --- the closure-check quote -------------------------------------------------


@pytest.fixture
def restaurants(tmp_path, monkeypatch):
    path = str(tmp_path / "quote.db")
    monkeypatch.setattr(
        db, "settings", dataclasses.replace(db.settings, database_path=path)
    )

    def seed(due: int, fresh: int = 0):
        db.init_db()
        db.upsert_restaurants(
            [
                {"place_id": f"p{i}", "name": f"Cafe {i}", "address": "A",
                 "lat": 1, "lng": 1, "website_url": None, "last_scraped_at": "t"}
                for i in range(due + fresh)
            ]
        )
        now = datetime.now(timezone.utc)
        for row in db.list_restaurants():
            age = 40 if int(row["place_id"][1:]) < due else 1
            db.update_place_status(
                row["id"], checked_at=(now - timedelta(days=age)).isoformat()
            )

    return seed


def test_endpoint_quotes_the_next_closure_check(restaurants):
    restaurants(due=4, fresh=2)
    gu.set_count(gu.DETAILS_PRO, 4_999)
    body = api.app.test_client().get("/api/google-usage").get_json()
    quote = body["refresh"]
    assert (quote["due"], quote["calls"], quote["billable"]) == (4, 4, 3)
    assert quote["cost"] == 0.05
    assert quote["tier"] == "Place Details Pro"
    assert (quote["stale_days"], quote["limit"]) == (28, 1_500)
    assert {s["id"] for s in body["skus"]} == set(gu.SKUS)


def test_quote_respects_the_per_click_cap(restaurants, monkeypatch):
    restaurants(due=5)
    monkeypatch.setattr(enrich, "REFRESH_MAX_CALLS", 3)
    quote = enrich.refresh_estimate()
    assert (quote["due"], quote["calls"]) == (5, 3)


def test_quote_matches_what_the_refresh_then_calls(restaurants, monkeypatch):
    restaurants(due=3, fresh=2)
    quote = enrich.refresh_estimate()
    monkeypatch.setattr(enrich, "_REFRESH_CALLS_PER_SECOND", 1_000_000.0)
    monkeypatch.setattr(
        enrich, "settings",
        dataclasses.replace(enrich.settings, google_places_api_key="k"),
    )
    calls = []

    def status(place_id, *, api_key, client):
        calls.append(place_id)
        return {"name": None, "address": None, "lat": None, "lng": None,
                "business_status": "OPERATIONAL", "moved_place_id": None}

    monkeypatch.setattr(enrich, "fetch_place_status", status)
    enrich.refresh(stale_days=quote["stale_days"], limit=quote["limit"])
    assert len(calls) == quote["calls"]
    assert enrich.refresh_estimate()["calls"] == 0
