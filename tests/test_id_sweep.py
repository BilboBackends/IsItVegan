"""Free ID sweep: tiling, pagination, dedupe, endpoints, website backfill."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api  # noqa: E402
import db  # noqa: E402
import places_client as pc  # noqa: E402


def test_search_masks_stay_pro_tier():
    # websiteUri in a SEARCH mask silently changes the billing SKU from Pro
    # (5k free/month) to Enterprise (1k free/month). It belongs only in the
    # enrichment Details mask, which is already at a higher tier.
    assert "websiteUri" not in pc.FIELD_MASK
    assert "websiteUri" not in pc.SUMMARY_FIELD_MASK
    assert pc.IDS_ONLY_FIELD_MASK == "places.id,nextPageToken"
    assert "websiteUri" in pc._DETAILS_FIELD_MASK


def test_id_sweep_tiles_cover_circle_and_skip_far_corners():
    tiles = pc.id_sweep_tiles(28.5, -81.4, 5_000, 1_500)
    assert len(tiles) > 4
    for lo_lat, lo_lng, hi_lat, hi_lng in tiles:
        assert lo_lat < hi_lat and lo_lng < hi_lng
        c_lat, c_lng = (lo_lat + hi_lat) / 2, (lo_lng + hi_lng) / 2
        assert pc.distance_meters(28.5, -81.4, c_lat, c_lng) <= 5_000 + 1_500

    # A tiny circle still gets at least one tile.
    assert len(pc.id_sweep_tiles(28.5, -81.4, 300, 1_500)) >= 1


class _FakeResp:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_text_search_ids_paginates_and_stops_at_cap(monkeypatch):
    pages = [
        {"places": [{"id": f"a{i}"} for i in range(20)], "nextPageToken": "t1"},
        {"places": [{"id": f"b{i}"} for i in range(20)], "nextPageToken": "t2"},
        {"places": [{"id": f"c{i}"} for i in range(20)], "nextPageToken": "t3"},
    ]
    calls = []

    class FakeClient:
        def post(self, url, headers=None, json=None, timeout=None):
            calls.append(json)
            return _FakeResp(pages[len(calls) - 1])

    ids, n_calls = pc.text_search_ids(
        FakeClient(), "restaurants", (28.0, -81.5, 28.1, -81.4), api_key="fake"
    )
    # 60-result ceiling: three pages, then stop even though a token remained.
    assert n_calls == 3
    assert len(ids) == 60
    assert calls[1]["pageToken"] == "t1"


def test_text_search_ids_retries_past_rate_limit(monkeypatch):
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)
    responses = [
        _FakeResp({}, status_code=429),
        _FakeResp({"places": [{"id": "x1"}]}),
    ]

    class FakeClient:
        def post(self, url, headers=None, json=None, timeout=None):
            return responses.pop(0)

    ids, calls = pc.text_search_ids(
        FakeClient(), "food", (28.0, -81.5, 28.1, -81.4), api_key="fake"
    )
    assert ids == ["x1"]
    assert calls == 2  # the 429 is counted, then the retry succeeds


def test_id_sweep_merges_and_dedupes(monkeypatch):
    def fake_search(client, query, rect, *, api_key, timeout, gate=None):
        if query == "restaurants":
            return ["shared", "resto1"], 1
        return ["shared", "cafe1"], 1

    monkeypatch.setattr(pc, "text_search_ids", fake_search)
    result = pc.id_sweep(
        api_key="fake",
        lat=28.5,
        lng=-81.4,
        radius_meters=500,
        queries=("restaurants", "cafes"),
        workers=1,
    )
    assert set(result["place_ids"]) == {"shared", "resto1", "cafe1"}
    assert result["id_count"] == 3
    assert result["calls_run"] == 2 * result["tiles"]


def test_id_sweep_endpoint_diffs_against_db(monkeypatch):
    monkeypatch.setattr(
        api,
        "settings",
        dataclasses.replace(api.settings, google_places_api_key="fake"),
    )
    monkeypatch.setattr(api.db, "init_db", lambda: None)
    monkeypatch.setattr(
        api.db,
        "list_restaurants",
        lambda: [{"place_id": "known1", "id": 5, "archived": 0}],
    )
    monkeypatch.setattr(
        pc,
        "id_sweep",
        lambda **kw: {
            "place_ids": ["known1", "new1", "new2"],
            "id_count": 3,
            "tiles": 4,
            "queries": 6,
            "calls_run": 24,
            "saturated_queries": 0,
            "errors": [],
        },
    )
    client = api.app.test_client()
    response = client.post(
        "/api/prospect/ids",
        json={"lat": 28.5, "lng": -81.4, "radius_meters": 1_000},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["new_ids"] == ["new1", "new2"]
    assert data["new_count"] == 2
    assert data["known_count"] == 1
    assert "place_ids" not in data  # only the diff leaves the endpoint

    invalid = client.post(
        "/api/prospect/ids", json={"lat": True, "lng": -81.4, "radius_meters": 5}
    )
    assert invalid.status_code == 400


def test_hydrate_endpoint_marks_and_sorts(monkeypatch):
    monkeypatch.setattr(
        api,
        "settings",
        dataclasses.replace(api.settings, google_places_api_key="fake"),
    )
    monkeypatch.setattr(api.db, "init_db", lambda: None)
    monkeypatch.setattr(api.db, "list_restaurants", lambda: [])

    def fake_summary(pid, *, api_key):
        return {
            "place_id": pid,
            "name": f"Zed {pid}" if pid == "p1" else f"Alpha {pid}",
            "address": "Somewhere",
            "website_url": None,
            "lat": 28.5,
            "lng": -81.4,
            # Free-text sweeps match non-food businesses too; hydration must
            # drop them once their primaryType reveals what they are.
            "primary_type": "car_repair" if pid == "junk" else "restaurant",
            "business_status": "OPERATIONAL",
        }

    monkeypatch.setattr(pc, "fetch_place_summary", fake_summary)
    client = api.app.test_client()
    response = client.post(
        "/api/prospect/ids/hydrate", json={"place_ids": ["p1", "p2", "junk"]}
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["count"] == 2
    assert data["new_count"] == 2
    assert data["dropped_non_food"] == 1
    assert data["places"][0]["name"].startswith("Alpha")

    invalid = client.post("/api/prospect/ids/hydrate", json={"place_ids": []})
    assert invalid.status_code == 400


def test_enrichment_website_fills_but_never_overwrites(tmp_path):
    path = str(tmp_path / "web.db")
    db.init_db(path)
    db.upsert_restaurants(
        [
            {"place_id": "g1", "name": "No Site", "address": "A", "lat": 1,
             "lng": 1, "website_url": None, "last_scraped_at": "t"},
            {"place_id": "g2", "name": "Has Site", "address": "B", "lat": 1,
             "lng": 1, "website_url": "https://curated.example.com",
             "last_scraped_at": "t"},
        ],
        db_path=path,
    )
    rows = {r["place_id"]: r for r in db.list_restaurants(db_path=path)}
    common = dict(
        serves_vegetarian=None, price_level=None, primary_type=None,
        editorial_summary=None, rating=None, user_rating_count=0,
        open_now=None, opening_hours=[], enriched_at="now",
        business_status="OPERATIONAL", db_path=path,
    )
    db.update_food_signals(
        rows["g1"]["id"], website_url="https://google.example.com", **common
    )
    db.update_food_signals(
        rows["g2"]["id"], website_url="https://google.example.com", **common
    )
    rows = {r["place_id"]: r for r in db.list_restaurants(db_path=path)}
    assert rows["g1"]["website_url"] == "https://google.example.com"
    assert rows["g2"]["website_url"] == "https://curated.example.com"
