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

from venue_filter import is_consumer_food_venue

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
        "places.primaryType",
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
        "primary_type": place.get("primaryType"),
    }


# Place Details fields for enrichment. These are structured food signals Google
# returns for many (not all) restaurants — a free vegetarian prior + dish hints
# that don't depend on scraping the restaurant's own site.
_DETAILS_FIELD_MASK = ",".join(
    [
        "id",
        "priceLevel",
        "primaryType",
        "editorialSummary",
        "servesVegetarianFood",
        "rating",
        "userRatingCount",
        "currentOpeningHours",
    ]
)

PLACE_DETAILS_URL = "https://places.googleapis.com/v1/places/"

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"


def prospect_places(
    query: str,
    *,
    api_key: str,
    bias_lat: float | None = None,
    bias_lng: float | None = None,
    bias_radius_meters: float = 50_000.0,
    max_results: int = 60,
    timeout: float = 30.0,
) -> list[dict]:
    """Area prospecting: every place Text Search returns for a free-form
    query ("restaurants on Mills Ave Orlando"), paginated up to max_results.

    Nothing is persisted — this feeds the Admin prospect view, where a human
    picks which places actually enter the pipeline. Same normalized shape as
    discovery, so selections can go straight to add_restaurants.add_places.
    """
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK + ",nextPageToken",
    }
    body: dict = {"textQuery": query, "pageSize": 20}
    if bias_lat is not None and bias_lng is not None:
        body["locationBias"] = {
            "circle": {
                "center": {"latitude": bias_lat, "longitude": bias_lng},
                "radius": bias_radius_meters,
            }
        }

    results: list[dict] = []
    seen: set[str] = set()
    with httpx.Client(timeout=timeout) as client:
        page_token: str | None = None
        while len(results) < max_results:
            payload = dict(body)
            if page_token:
                payload["pageToken"] = page_token
            resp = client.post(TEXT_SEARCH_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            for place in data.get("places", []):
                normalized = _normalize_place(place)
                if normalized["place_id"] in seen:
                    continue
                seen.add(normalized["place_id"])
                results.append(normalized)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return results[:max_results]


def search_place_candidates(
    name: str,
    *,
    api_key: str,
    bias_lat: float | None = None,
    bias_lng: float | None = None,
    bias_radius_meters: float = 50_000.0,
    timeout: float = 30.0,
) -> list[dict]:
    """All Text Search candidates for a name, best first, for user selection.

    The bias circle nudges results toward our area ("Antonio's" finds the
    Maitland one) without restricting — an explicit query like "Antonio's
    Orlando" still wins. Each candidate carries `name_overlap`: whether its
    name plausibly IS the queried restaurant. (Text Search happily returns a
    semantically-similar place when the exact one isn't nearby — e.g. a
    different vegan restaurant — so the UI marks those as weak matches.)
    """
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body: dict = {
        "textQuery": name,
        "maxResultCount": 5,
    }
    if bias_lat is not None and bias_lng is not None:
        body["locationBias"] = {
            "circle": {
                "center": {"latitude": bias_lat, "longitude": bias_lng},
                "radius": bias_radius_meters,
            }
        }

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(TEXT_SEARCH_URL, headers=headers, json=body)
        resp.raise_for_status()
        places = resp.json().get("places", [])

    candidates = [_normalize_place(p) for p in places]
    for cand in candidates:
        cand["name_overlap"] = _names_overlap(name, cand["name"])
    return candidates


def search_place_by_name(
    name: str,
    *,
    api_key: str,
    bias_lat: float | None = None,
    bias_lng: float | None = None,
    bias_radius_meters: float = 50_000.0,
    timeout: float = 30.0,
) -> dict | None:
    """Resolve a restaurant name to a place via Text Search. Top match or None.

    The unattended flavor of search_place_candidates (CLI/scripted adds):
    keeps only the first candidate whose name genuinely overlaps the query.
    """
    for cand in search_place_candidates(
        name,
        api_key=api_key,
        bias_lat=bias_lat,
        bias_lng=bias_lng,
        bias_radius_meters=bias_radius_meters,
        timeout=timeout,
    ):
        if cand["name_overlap"]:
            return cand
    return None


def _names_overlap(query: str, result_name: str) -> bool:
    """True if the result's name plausibly IS the queried restaurant."""
    q = query.lower()
    r = result_name.lower()
    if q in r or r in q:
        return True
    q_tokens = {t for t in q.replace("'", " ").split() if len(t) > 3}
    r_tokens = {t for t in r.replace("'", " ").split() if len(t) > 3}
    return bool(q_tokens & r_tokens)


def fetch_place_details(
    place_id: str, *, api_key: str, timeout: float = 30.0
) -> dict:
    """Fetch structured food signals for one place. Missing fields -> None.

    Returns food signals plus Google rating and rating count.
    """
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": _DETAILS_FIELD_MASK}
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(f"{PLACE_DETAILS_URL}{place_id}", headers=headers)
        resp.raise_for_status()
        data = resp.json()

    editorial = (data.get("editorialSummary") or {}).get("text")
    hours = data.get("currentOpeningHours") or {}
    return {
        "serves_vegetarian": data.get("servesVegetarianFood"),  # True/False/None
        "price_level": data.get("priceLevel"),
        "primary_type": data.get("primaryType"),
        "editorial_summary": editorial,
        "rating": data.get("rating"),
        # Treat a missing count as zero so enrichment records that ratings
        # were checked; otherwise unrated places would be fetched every run.
        "user_rating_count": data.get("userRatingCount", 0),
        "open_now": hours.get("openNow"),
        "opening_hours": hours.get("weekdayDescriptions") or [],
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


def _in_city(restaurant: dict, city: str) -> bool:
    """True if the restaurant's formatted address is in the target city.

    Matches the city as a comma-delimited address component (", Maitland,")
    so a street named after another town doesn't create false positives.
    """
    address = restaurant.get("address") or ""
    return f", {city.lower()}," in f" {address.lower()} "


def discover_restaurants(
    *,
    api_key: str | None,
    lat: float,
    lng: float,
    radius_meters: float,
    cell_radius_meters: float = 800.0,
    city_filter: str | None = None,
    mock_fixture_path: str | None = None,
) -> dict:
    """Discover restaurants covering the area; optionally filter to a city.

    Tiles the area (centered on lat/lng, out to radius_meters) with a grid of
    smaller search circles of cell_radius_meters each, since one call returns
    at most 20 results. Results are deduped by place_id.

    If city_filter is set, results whose address is not in that city are
    dropped (the area search overshoots small towns into neighbors).

    Returns a dict: {"restaurants": [...], "raw_count": int, "dropped": int}
    so callers can log how many out-of-city places were filtered.

    If mock_fixture_path is set, reads places from that JSON file and skips the
    network entirely — no grid.
    """
    if mock_fixture_path:
        raw = json.loads(Path(mock_fixture_path).read_text(encoding="utf-8"))
        places = raw.get("places", raw) if isinstance(raw, dict) else raw
        found = _dedup([_normalize_place(p) for p in places])
    elif not api_key:
        raise RuntimeError(
            "GOOGLE_PLACES_API_KEY is not set. Put it in .env, or run with a "
            "mock fixture (see fixtures/maitland_sample.json)."
        )
    else:
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
        found = _dedup(normalized)

    raw_count = len(found)
    found = [place for place in found if is_consumer_food_venue(place)]
    if city_filter:
        kept = [r for r in found if _in_city(r, city_filter)]
    else:
        kept = found

    return {
        "restaurants": kept,
        "raw_count": raw_count,
        "dropped": raw_count - len(kept),
    }


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
