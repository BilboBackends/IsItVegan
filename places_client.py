"""Google Places API (New) client for restaurant discovery.

Wraps the Places API (New) "Nearby Search" endpoint. Uses a field mask to
request only the fields we persist in Phase 0 (name, address, place_id,
website, location) — field masks control billing SKUs, so we keep it minimal.

Mock-first: pass mock_fixture_path to read a local JSON fixture instead of
hitting the live API, so the discovery stage is runnable/testable without a
key or network. See fixtures/maitland_sample.json.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"

# Only the fields we store in Phase 0. Adding fields here can change billing.
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.websiteUri",
        "places.location",
    ]
)


def _normalize_place(place: dict) -> dict:
    """Map a Places API (New) place object to our restaurant row shape."""
    location = place.get("location") or {}
    display_name = place.get("displayName") or {}
    return {
        "place_id": place["id"],
        "name": display_name.get("text") or place.get("id"),
        "address": place.get("formattedAddress"),
        "website_url": place.get("websiteUri"),
        "lat": location.get("latitude"),
        "lng": location.get("longitude"),
    }


def discover_restaurants(
    *,
    api_key: str | None,
    lat: float,
    lng: float,
    radius_meters: float,
    max_results: int = 20,
    mock_fixture_path: str | None = None,
) -> list[dict]:
    """Return normalized restaurant dicts near (lat, lng).

    If mock_fixture_path is set, reads places from that JSON file (a list of
    raw Places API place objects) and skips the network entirely.
    """
    if mock_fixture_path:
        raw = json.loads(Path(mock_fixture_path).read_text(encoding="utf-8"))
        places = raw.get("places", raw) if isinstance(raw, dict) else raw
        return [_normalize_place(p) for p in places]

    if not api_key:
        raise RuntimeError(
            "GOOGLE_PLACES_API_KEY is not set. Put it in .env, or run with a "
            "mock fixture (see fixtures/maitland_sample.json)."
        )

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body = {
        "includedTypes": ["restaurant"],
        # Nearby Search (New) caps at 20 results per call; for a small area
        # this is usually enough. Broader coverage later = multiple centers
        # or Text Search with pagination.
        "maxResultCount": min(max_results, 20),
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": radius_meters,
            }
        },
    }

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(NEARBY_SEARCH_URL, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()

    places = data.get("places", [])
    return [_normalize_place(p) for p in places]
