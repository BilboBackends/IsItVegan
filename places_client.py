"""Google Places API (New) client for restaurant discovery.

Wraps the Places API (New) "Nearby Search" endpoint. Uses a field mask to
request only the fields we persist in Phase 0 (name, address, place_id,
website, location) — field masks control billing SKUs, so we keep it minimal.

Nearby Search (New) returns at most 20 results per call and has no paging
token. To cover an area larger than one dense 20-result circle, we tile the
bounding box with a grid of overlapping search circles and dedup by place_id.

Mock-first: pass mock_fixture_path to read a local JSON fixture instead of
hitting the live API, so the discovery stage is runnable/testable without a
key or network. See fixtures/maitland_sample.json.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import httpx

# Max results a single Nearby Search (New) call can return.
_PER_CALL_CAP = 20

# Rough meters-per-degree conversions for building the search grid. Latitude
# is ~constant; longitude shrinks toward the poles, so scale by cos(lat).
_METERS_PER_DEG_LAT = 111_320.0


def _meters_per_deg_lng(lat: float) -> float:
    return _METERS_PER_DEG_LAT * math.cos(math.radians(lat))

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


def _search_circle(
    client: httpx.Client,
    *,
    api_key: str,
    lat: float,
    lng: float,
    radius_meters: float,
) -> list[dict]:
    """One Nearby Search call. Returns raw (un-normalized) place objects."""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body = {
        "includedTypes": ["restaurant"],
        "maxResultCount": _PER_CALL_CAP,
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": radius_meters,
            }
        },
    }
    resp = client.post(NEARBY_SEARCH_URL, headers=headers, json=body)
    resp.raise_for_status()
    return resp.json().get("places", [])


def _grid_centers(
    lat: float, lng: float, area_radius_meters: float, cell_radius_meters: float
) -> list[tuple[float, float]]:
    """Tile the bounding box around (lat, lng) with overlapping cell centers.

    Centers are spaced at cell_radius (so adjacent circles of that radius
    overlap ~2x), guaranteeing coverage with no gaps between circles.
    """
    step = cell_radius_meters
    dlat = step / _METERS_PER_DEG_LAT
    dlng = step / _meters_per_deg_lng(lat)

    # How many steps from center to edge of the area, in each direction.
    n = max(1, math.ceil(area_radius_meters / step))

    centers: list[tuple[float, float]] = []
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            centers.append((lat + i * dlat, lng + j * dlng))
    return centers


def discover_restaurants(
    *,
    api_key: str | None,
    lat: float,
    lng: float,
    radius_meters: float,
    cell_radius_meters: float = 800.0,
    mock_fixture_path: str | None = None,
) -> list[dict]:
    """Return deduplicated, normalized restaurant dicts covering the area.

    Tiles the area (centered on lat/lng, out to radius_meters) with a grid of
    smaller search circles of cell_radius_meters each, since one call returns
    at most 20 results. Results are deduped by place_id.

    If mock_fixture_path is set, reads places from that JSON file (a list of
    raw Places API place objects) and skips the network entirely — no grid.
    """
    if mock_fixture_path:
        raw = json.loads(Path(mock_fixture_path).read_text(encoding="utf-8"))
        places = raw.get("places", raw) if isinstance(raw, dict) else raw
        return _dedup([_normalize_place(p) for p in places])

    if not api_key:
        raise RuntimeError(
            "GOOGLE_PLACES_API_KEY is not set. Put it in .env, or run with a "
            "mock fixture (see fixtures/maitland_sample.json)."
        )

    centers = _grid_centers(lat, lng, radius_meters, cell_radius_meters)
    normalized: list[dict] = []
    with httpx.Client(timeout=30.0) as client:
        for c_lat, c_lng in centers:
            raw_places = _search_circle(
                client,
                api_key=api_key,
                lat=c_lat,
                lng=c_lng,
                radius_meters=cell_radius_meters,
            )
            normalized.extend(_normalize_place(p) for p in raw_places)

    return _dedup(normalized)


def _dedup(restaurants: list[dict]) -> list[dict]:
    """Keep the first occurrence of each place_id, preserving order."""
    seen: set[str] = set()
    out: list[dict] = []
    for r in restaurants:
        pid = r["place_id"]
        if pid not in seen:
            seen.add(pid)
            out.append(r)
    return out
