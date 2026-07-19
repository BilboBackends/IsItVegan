"""Overture free-sweep filtering, known-place marking, add-flow resolution."""
from __future__ import annotations

import dataclasses
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import add_restaurants  # noqa: E402
import api  # noqa: E402
import db  # noqa: E402
import overture_client as oc  # noqa: E402


def _wkb(lng: float, lat: float) -> bytes:
    return struct.pack("<BIdd", 1, 1, lng, lat)


def _row(
    name: str,
    lng: float,
    lat: float,
    category: str = "restaurant",
    confidence: float = 0.9,
    **extra,
) -> dict:
    row = {
        "id": f"ov-{name}",
        "geometry": _wkb(lng, lat),
        "names": {"primary": name},
        "categories": {"primary": category},
        "confidence": confidence,
        "websites": [f"https://{name}.example.com"],
        "addresses": [
            {
                "freeform": "1 Main St",
                "locality": "Maitland",
                "region": "FL",
                "postcode": "32751",
            }
        ],
    }
    row.update(extra)
    return row


def test_fetch_filters_category_confidence_status_and_radius():
    rows = [
        _row("keeper", -81.4, 28.5),
        _row("taco", -81.4001, 28.5005, category="mexican_restaurant"),
        _row("grocery", -81.4, 28.5, category="grocery_store"),
        _row("noise", -81.4, 28.5, confidence=0.2),
        _row("gone", -81.4005, 28.5, operating_status="permanently_closed"),
        _row("faraway", -81.5, 28.5),  # ~9.8 km west, outside the 1 km circle
    ]
    result = oc.fetch_overture_food_places(28.5, -81.4, 1_000, rows=rows)

    assert {p["name"] for p in result["places"]} == {"keeper", "taco"}
    assert result["dropped_non_food"] == 1
    assert result["dropped_low_confidence"] == 1
    assert result["dropped_closed"] == 1
    keeper = next(p for p in result["places"] if p["name"] == "keeper")
    assert keeper["place_id"] is None
    assert keeper["overture_id"] == "ov-keeper"
    assert keeper["source"] == "overture"
    assert keeper["website_url"] == "https://keeper.example.com"
    assert keeper["address"] == "1 Main St, Maitland, FL 32751"
    assert keeper["distance_meters"] == 0


def test_fetch_collapses_near_duplicates_keeping_most_confident():
    rows = [
        _row("Taco Palace", -81.4, 28.5, confidence=0.7),
        _row("Taco Palace Restaurant", -81.40001, 28.50001, confidence=0.95),
        _row("Different Diner", -81.4, 28.5, confidence=0.9),
    ]
    result = oc.fetch_overture_food_places(28.5, -81.4, 1_000, rows=rows)
    names = {p["name"] for p in result["places"]}
    assert names == {"Taco Palace Restaurant", "Different Diner"}


def test_fetch_rejects_bad_inputs():
    for lat, lng, radius in ((95, -81.4, 1_000), (28.5, -81.4, 100)):
        try:
            oc.fetch_overture_food_places(lat, lng, radius, rows=[])
        except ValueError:
            pass
        else:
            raise AssertionError("invalid sweep inputs should be rejected")


def test_mark_known_places_needs_proximity_and_name_overlap():
    places = [
        {"name": "Taco Palace", "lat": 28.5, "lng": -81.4},
        {"name": "Bagel Barn", "lat": 28.51, "lng": -81.4},
        {"name": "Sushi Spot", "lat": 28.5, "lng": -81.4},
    ]
    existing = [
        {"id": 7, "name": "Taco Palace", "lat": 28.50001, "lng": -81.40001,
         "archived": 0},
        # Same name but ~1.1 km away — a different location, not a match.
        {"id": 8, "name": "Bagel Barn", "lat": 28.52, "lng": -81.4, "archived": 0},
        # Same spot, unrelated name (e.g. its food-hall neighbor).
        {"id": 9, "name": "Burger Bros", "lat": 28.5, "lng": -81.4, "archived": 0},
    ]
    new_count = oc.mark_known_places(places, existing)
    assert new_count == 2
    assert places[0]["already_added_id"] == 7
    assert places[1]["already_added_id"] is None
    assert places[2]["already_added_id"] is None


def test_overture_endpoint_marks_new_places(monkeypatch):
    monkeypatch.setattr(api.db, "init_db", lambda: None)
    monkeypatch.setattr(
        api.db,
        "list_restaurants",
        lambda: [
            {"id": 3, "name": "Taco Palace", "lat": 28.5, "lng": -81.4,
             "archived": 0}
        ],
    )
    import overture_client

    monkeypatch.setattr(
        overture_client,
        "fetch_overture_food_places",
        lambda lat, lng, radius_meters, min_confidence: {
            "places": [
                {"place_id": None, "overture_id": "ov-1", "name": "Taco Palace",
                 "lat": 28.50001, "lng": -81.40001, "source": "overture"},
                {"place_id": None, "overture_id": "ov-2", "name": "New Cafe",
                 "lat": 28.501, "lng": -81.4, "source": "overture"},
            ],
            "count": 2,
            "source": "overture",
            "scanned": 10,
            "dropped_non_food": 0,
            "dropped_low_confidence": 0,
            "dropped_closed": 0,
            "min_confidence": min_confidence,
        },
    )
    client = api.app.test_client()
    response = client.post(
        "/api/prospect/overture",
        json={"lat": 28.5, "lng": -81.4, "radius_meters": 1_000},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["new_count"] == 1
    assert data["places"][0]["already_added_id"] == 3
    assert data["places"][1]["already_added_id"] is None

    invalid = client.post(
        "/api/prospect/overture",
        json={"lat": 28.5, "lng": -81.4, "radius_meters": 1_000,
              "min_confidence": 2},
    )
    assert invalid.status_code == 400


def test_resolve_external_place_gates_on_name_and_distance(monkeypatch):
    monkeypatch.setattr(
        add_restaurants,
        "settings",
        dataclasses.replace(add_restaurants.settings, google_places_api_key="fake"),
    )
    captured = {}

    def fake_candidates(query, **kwargs):
        captured["query"] = query
        captured["field_mask"] = kwargs.get("field_mask")
        return [
            # Name matches but it's the same-name sibling across town.
            {"place_id": "g-far", "name": "Taco Palace", "lat": 28.6,
             "lng": -81.4, "name_overlap": True},
            # Nearby but a different venue Google offered anyway.
            {"place_id": "g-other", "name": "Sushi Spot", "lat": 28.5001,
             "lng": -81.4, "name_overlap": True},
            {"place_id": "g-right", "name": "Taco Palace", "lat": 28.5001,
             "lng": -81.4001, "name_overlap": True},
        ]

    monkeypatch.setattr(add_restaurants, "search_place_candidates", fake_candidates)
    place = {"name": "Taco Palace", "address": "1 Main St, Maitland, FL",
             "lat": 28.5, "lng": -81.4}
    resolved = add_restaurants.resolve_external_place(place)
    assert resolved["place_id"] == "g-right"
    assert captured["query"] == "Taco Palace, 1 Main St, Maitland, FL"
    from places_client import RESOLVE_FIELD_MASK

    assert captured["field_mask"] == RESOLVE_FIELD_MASK

    monkeypatch.setattr(
        add_restaurants, "search_place_candidates", lambda *a, **k: []
    )
    assert add_restaurants.resolve_external_place(place) is None


def test_add_places_tags_overture_rows_and_defers_enrichment(tmp_path, monkeypatch):
    path = str(tmp_path / "overture-add.db")
    monkeypatch.setattr(
        db, "settings", dataclasses.replace(db.settings, database_path=path)
    )
    monkeypatch.setattr(
        add_restaurants,
        "resolve_external_place",
        lambda place: {
            "place_id": "g-1",
            "name": "Taco Palace",
            "address": "1 Main St, Maitland, FL",
            "lat": 28.5,
            "lng": -81.4,
            "primary_type": "mexican_restaurant",
            "website_url": None,
        },
    )
    enrich_calls = []
    monkeypatch.setattr(
        add_restaurants.enrich, "run", lambda **kw: enrich_calls.append(kw)
    )

    result = add_restaurants.add_places(
        [
            {
                "overture_id": "ov-1",
                "name": "Taco Palace",
                "website_url": "https://taco.example.com",
                "lat": 28.5,
                "lng": -81.4,
            }
        ],
        do_ingest=False,
        do_classify=False,
    )

    # Overture adds defer the metered Details call; the Admin pending panel
    # (or any normal enrich run) picks them up later.
    assert enrich_calls == []
    entry = result["added"][0]
    assert entry["id"] is not None
    assert entry["enrich_deferred"] is True
    row = db.list_restaurants()[0]
    assert row["place_id"] == "g-1"
    assert row["discovery_source"] == "overture"
    assert row["website_url"] == "https://taco.example.com"
    assert row["enriched_at"] is None
