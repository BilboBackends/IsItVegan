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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

from venue_filter import is_consumer_food_venue

# Max results a single Nearby Search (New) call can return.
_PER_CALL_CAP = 20

# Rough meters-per-degree conversions for building the search grid. Latitude
# is ~constant; longitude shrinks toward the poles, so scale by cos(lat).
_METERS_PER_DEG_LAT = 111_320.0

# A broad restaurant search catches cuisine-specific restaurant types because
# Google tags those places with the general ``restaurant`` type as well. The
# second request catches food businesses that are commonly missing from a
# restaurant-only sweep: breweries, cafes, bakeries, dessert/ice-cream shops,
# and food-oriented pubs. Keeping these separate prevents a dense restaurant
# result set from crowding every non-restaurant venue out of the 20-place cap.
RADIUS_FOOD_TYPE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("restaurants", ("restaurant",)),
    (
        "other_food",
        (
            "acai_shop", "bagel_shop", "bakery", "bar_and_grill",
            "beer_garden", "brewery", "brewpub", "cafe", "cafeteria",
            "cake_shop", "coffee_roastery", "coffee_shop", "coffee_stand",
            "deli", "dessert_shop", "donut_shop", "food_court", "gastropub",
            "ice_cream_shop", "juice_shop", "meal_takeaway", "pastry_shop",
            "pub", "sandwich_shop", "snack_bar", "sports_bar", "tea_house",
            "winery",
        ),
    ),
)

RADIUS_SWEEP_CELL_METERS = 1_000.0
RADIUS_SWEEP_MAX_CALLS = 1_500
# FIELD_MASK is all Pro-tier fields, so the Nearby sweep bills as Nearby
# Search Pro (5k free calls/month). Websites come from enrichment instead.
RADIUS_SWEEP_PRICE_PER_CALL = 0.032  # Current list price: $32/1k calls.


def _meters_per_deg_lng(lat: float) -> float:
    return _METERS_PER_DEG_LAT * math.cos(math.radians(lat))

NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"

# Search-result fields for prospect/sweep/resolve flows. All Pro-tier: the
# field mask decides the billing SKU, and websiteUri would bump every search
# to Enterprise (1k free calls/month vs Pro's 5k). Websites now arrive via
# the enrichment Details call instead (websiteUri is BELOW that call's
# existing Enterprise+Atmosphere tier, so it rides along free there).
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.primaryType",
    ]
)

# Text Search "Essentials (IDs Only)" SKU — unlimited free. The ID sweep
# uses this to discover WHAT exists at zero cost; only ids never seen before
# get a paid (free-tier Pro) Details hydration.
IDS_ONLY_FIELD_MASK = "places.id,nextPageToken"

# Pro-tier Place Details mask for hydrating unknown ids: everything the
# Prospect list displays, nothing from the Enterprise tiers.
SUMMARY_FIELD_MASK = ",".join(
    ["id", "displayName", "formattedAddress", "location", "primaryType",
     "businessStatus"]
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
        # OPERATIONAL | CLOSED_TEMPORARILY | CLOSED_PERMANENTLY — how we
        # notice a restaurant went out of business (Ethos) without a human
        # stumbling onto it.
        "businessStatus",
        # Search masks stopped requesting websiteUri (it forced the Enterprise
        # SKU there); this call already bills Enterprise+Atmosphere, so the
        # website rides along at no extra tier and enrichment persists it.
        "websiteUri",
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
        "business_status": data.get("businessStatus"),
        "website_url": data.get("websiteUri"),
    }


def fetch_place_photo_names(
    place_id: str, *, api_key: str, timeout: float = 30.0
) -> list[str]:
    """Photo resource names for a place (places/X/photos/Y), newest first."""
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": "photos"}
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(f"{PLACE_DETAILS_URL}{place_id}", headers=headers)
        resp.raise_for_status()
        photos = resp.json().get("photos") or []
    return [p.get("name") for p in photos if p.get("name")]


def download_place_photo(
    photo_name: str,
    *,
    api_key: str,
    max_width_px: int = 1600,
    timeout: float = 30.0,
) -> tuple[bytes, str]:
    """(bytes, media_type) for one place photo via the media endpoint."""
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(
            f"https://places.googleapis.com/v1/{photo_name}/media",
            params={"key": api_key, "maxWidthPx": max_width_px},
        )
        resp.raise_for_status()
    media_type = (
        resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    )
    return resp.content, media_type


def _search_circle(
    client: httpx.Client,
    *,
    api_key: str,
    lat: float,
    lng: float,
    radius_meters: float,
    included_types: tuple[str, ...] = ("restaurant",),
) -> list[dict]:
    """One Nearby Search call. Returns raw (un-normalized) place objects."""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body = {
        "includedTypes": list(included_types),
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


def distance_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two WGS84 points."""
    earth_radius = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = p2 - p1
    dlng = math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlng / 2) ** 2
    )
    return earth_radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def radius_grid_centers(
    lat: float,
    lng: float,
    radius_meters: float,
    cell_radius_meters: float = RADIUS_SWEEP_CELL_METERS,
) -> list[tuple[float, float]]:
    """Overlapping Nearby Search cells that cover one exact outer circle."""
    if radius_meters <= cell_radius_meters:
        return [(lat, lng)]
    centers = _grid_centers(lat, lng, radius_meters, cell_radius_meters)
    # Keep cells that touch the selected outer circle. Results are filtered
    # back to the exact outer radius after Google returns them.
    return [
        center
        for center in centers
        if distance_meters(lat, lng, center[0], center[1])
        <= radius_meters + cell_radius_meters
    ]


def estimate_radius_food_sweep(
    lat: float,
    lng: float,
    radius_meters: float,
    *,
    cell_radius_meters: float = RADIUS_SWEEP_CELL_METERS,
) -> dict:
    """Pure preflight estimate; performs no Google API requests."""
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise ValueError("Sweep center is outside valid latitude/longitude bounds.")
    if not 250 <= radius_meters <= 50_000:
        raise ValueError("Sweep radius must be between 250 m and 50 km.")
    centers = radius_grid_centers(lat, lng, radius_meters, cell_radius_meters)
    base_calls = len(centers) * len(RADIUS_FOOD_TYPE_GROUPS)
    return {
        "lat": lat,
        "lng": lng,
        "radius_meters": radius_meters,
        "cell_radius_meters": cell_radius_meters,
        "cells": len(centers),
        "type_groups": len(RADIUS_FOOD_TYPE_GROUPS),
        "base_calls": base_calls,
        "call_budget": min(RADIUS_SWEEP_MAX_CALLS, base_calls),
        "estimated_list_cost": round(base_calls * RADIUS_SWEEP_PRICE_PER_CALL, 2),
        "max_list_cost": round(
            min(RADIUS_SWEEP_MAX_CALLS, base_calls)
            * RADIUS_SWEEP_PRICE_PER_CALL,
            2,
        ),
        "within_call_limit": base_calls <= RADIUS_SWEEP_MAX_CALLS,
        "result_cap_per_call": _PER_CALL_CAP,
        "type_group_names": [group[0] for group in RADIUS_FOOD_TYPE_GROUPS],
    }


def radius_food_sweep(
    *,
    api_key: str,
    lat: float,
    lng: float,
    radius_meters: float,
    confirmed_call_budget: int,
    cell_radius_meters: float = RADIUS_SWEEP_CELL_METERS,
    timeout: float = 30.0,
    workers: int = 6,
) -> dict:
    """Find food venues within a user-selected circle, beyond one-call caps.

    The area is tiled into overlapping Nearby Search circles. Each cell runs
    a general restaurant query and a second query for non-restaurant food
    venues, then results are deduplicated and filtered to the exact requested
    radius. The explicit confirmed budget prevents accidental billing.
    """
    estimate = estimate_radius_food_sweep(
        lat, lng, radius_meters, cell_radius_meters=cell_radius_meters
    )
    if not estimate["within_call_limit"]:
        raise ValueError(
            f"This radius needs {estimate['base_calls']} base calls; the safety "
            f"limit is {RADIUS_SWEEP_MAX_CALLS}. Choose a smaller radius."
        )
    if confirmed_call_budget != estimate["call_budget"]:
        raise ValueError("Sweep estimate changed. Estimate again before running.")

    centers = radius_grid_centers(lat, lng, radius_meters, cell_radius_meters)
    tasks = [
        (c_lat, c_lng, group_name, included_types)
        for c_lat, c_lng in centers
        for group_name, included_types in RADIUS_FOOD_TYPE_GROUPS
    ]
    merged: dict[str, dict] = {}
    errors: list[str] = []
    saturated_calls = 0

    def search(
        client: httpx.Client,
        task: tuple[float, float, str, tuple[str, ...]],
    ):
        c_lat, c_lng, group_name, included_types = task
        places = _search_circle(
            client,
            api_key=api_key,
            lat=c_lat,
            lng=c_lng,
            radius_meters=cell_radius_meters,
            included_types=included_types,
        )
        return group_name, places

    # httpx.Client connection pools are thread-safe; sharing one avoids a new
    # TLS handshake for every grid cell.
    with httpx.Client(timeout=timeout) as client:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 10))) as pool:
            futures = {pool.submit(search, client, task): task for task in tasks}
            for future in as_completed(futures):
                task = futures[future]
                try:
                    group_name, raw_places = future.result()
                except Exception as exc:
                    errors.append(
                        f"{task[2]} at {task[0]:.5f},{task[1]:.5f}: {exc}"
                    )
                    continue
                if len(raw_places) >= _PER_CALL_CAP:
                    saturated_calls += 1
                for raw_place in raw_places:
                    place = _normalize_place(raw_place)
                    if place.get("lat") is None or place.get("lng") is None:
                        continue
                    distance = distance_meters(
                        lat, lng, place["lat"], place["lng"]
                    )
                    if distance > radius_meters or not is_consumer_food_venue(place):
                        continue
                    place["distance_meters"] = round(distance)
                    place["matched_group"] = group_name
                    merged.setdefault(place["place_id"], place)

    places = sorted(
        merged.values(),
        key=lambda place: (place.get("distance_meters", math.inf), place["name"].lower()),
    )
    return {
        "places": places,
        "count": len(places),
        "calls_run": len(tasks),
        "cells": len(centers),
        "saturated_calls": saturated_calls,
        "errors": errors,
        "estimate": estimate,
    }


# ---------------------------------------------------------------------------
# Free ID sweep: discovery decoupled from data.
#
# Phase 1 (free, unlimited): tile the area with Text Search rectangles using
# the IDs-only mask and a category battery, collecting place ids.
# Phase 2 (caller): diff ids against the DB — only ids we've never seen need
# hydration (Details, Pro tier, 5k free/month), and only places the user
# ADDS trigger the full enrichment call. Cost scales with new restaurants,
# not with area swept, and every result is Google-live (no stale entries).
# ---------------------------------------------------------------------------

# Battery tuned empirically against the Maitland ground truth (DB rows from
# exhaustive paid Nearby sweeps): this set + 1 km tiles re-found 33/33
# operational restaurants (100% recall). Two hard-won findings baked in:
# singular "restaurant" outperforms "restaurants", and mealtime/intent words
# ("lunch", "takeout") surface venues that category words rank out. Text
# Search returns RELEVANT matches, not all matches — even below the 60-result
# cap — so recall comes from query diversity, not bigger tiles.
ID_SWEEP_QUERIES: tuple[str, ...] = (
    "restaurant",
    "food",
    "places to eat",
    "takeout",
    "lunch",
    "dinner",
    "breakfast",
    "fast food",
    "coffee shops",
    "bakery",
    "dessert",
    "bars",
    "sandwiches",
)

# Rectangle side. Smaller tiles reduce per-tile ranking competition (which,
# not saturation, is what loses venues); 1 km scored 100% recall where
# 1.5 km missed two. Calls are free — tile count only costs time.
ID_SWEEP_TILE_METERS = 1_000.0

# One query on one tile pages up to this many results (Google's ceiling).
_ID_SWEEP_MAX_PER_QUERY = 60

# The IDs-only SKU is free of CHARGE but not of QUOTA: Text Search allows
# ~600 requests/minute per project, and an unthrottled multi-worker sweep
# 429s across the board (observed live: 3,133/3,133 failures). Pace below
# the quota and leave headroom for concurrent hydration/prospect calls.
ID_SWEEP_REQUESTS_PER_SECOND = 8.0


class _RateGate:
    """Serialize request starts to a fixed rate across worker threads."""

    def __init__(self, per_second: float):
        self._interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._interval
        if delay > 0:
            time.sleep(delay)


def id_sweep_tiles(
    lat: float,
    lng: float,
    radius_meters: float,
    tile_meters: float = ID_SWEEP_TILE_METERS,
) -> list[tuple[float, float, float, float]]:
    """(lo_lat, lo_lng, hi_lat, hi_lng) rectangles covering the sweep circle.

    Text Search restricts by rectangle (not circle), so the circle's bbox is
    cut into a grid and tiles whose center is beyond radius + half a tile
    diagonal are skipped.
    """
    dlat = tile_meters / _METERS_PER_DEG_LAT
    dlng = tile_meters / _meters_per_deg_lng(lat)
    n = max(1, math.ceil(radius_meters / tile_meters))
    reach = radius_meters + tile_meters * 0.75
    tiles = []
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            c_lat = lat + i * dlat
            c_lng = lng + j * dlng
            if distance_meters(lat, lng, c_lat, c_lng) > reach:
                continue
            tiles.append(
                (c_lat - dlat / 2, c_lng - dlng / 2,
                 c_lat + dlat / 2, c_lng + dlng / 2)
            )
    return tiles


def text_search_ids(
    client: httpx.Client,
    query: str,
    rect: tuple[float, float, float, float],
    *,
    api_key: str,
    timeout: float = 30.0,
    gate: _RateGate | None = None,
) -> tuple[list[str], int]:
    """(place ids, requests made) for one query on one tile. IDs-only = free."""
    lo_lat, lo_lng, hi_lat, hi_lng = rect
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": IDS_ONLY_FIELD_MASK,
    }
    body = {
        "textQuery": query,
        "pageSize": 20,
        "locationRestriction": {
            "rectangle": {
                "low": {"latitude": lo_lat, "longitude": lo_lng},
                "high": {"latitude": hi_lat, "longitude": hi_lng},
            }
        },
    }
    ids: list[str] = []
    calls = 0
    page_token: str | None = None
    while len(ids) < _ID_SWEEP_MAX_PER_QUERY:
        payload = dict(body)
        if page_token:
            payload["pageToken"] = page_token
        for attempt in range(4):
            if gate is not None:
                gate.wait()
            resp = client.post(
                TEXT_SEARCH_URL, headers=headers, json=payload, timeout=timeout
            )
            calls += 1
            # Free-of-charge is not free-of-quota: burst over ~600 req/min
            # answers 429. Back off and retry instead of losing the tile.
            if resp.status_code == 429:
                time.sleep(min(30.0, 5.0 * (2**attempt)))
                continue
            resp.raise_for_status()
            break
        else:
            raise RuntimeError("still rate-limited after 4 attempts")
        data = resp.json()
        ids.extend(p["id"] for p in data.get("places", []) if p.get("id"))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return ids, calls


def id_sweep(
    *,
    api_key: str,
    lat: float,
    lng: float,
    radius_meters: float,
    tile_meters: float = ID_SWEEP_TILE_METERS,
    queries: tuple[str, ...] = ID_SWEEP_QUERIES,
    timeout: float = 30.0,
    workers: int = 4,
) -> dict:
    """Collect every food-place id in the circle via free IDs-only searches."""
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise ValueError("Sweep center is outside valid latitude/longitude bounds.")
    if not 250 <= radius_meters <= 80_000:
        raise ValueError("Sweep radius must be between 250 m and 80 km.")
    tiles = id_sweep_tiles(lat, lng, radius_meters, tile_meters)
    tasks = [(tile, query) for tile in tiles for query in queries]
    ids: set[str] = set()
    calls_run = 0
    saturated = 0
    errors: list[str] = []

    gate = _RateGate(ID_SWEEP_REQUESTS_PER_SECOND)

    def search(client: httpx.Client, task):
        tile, query = task
        return text_search_ids(
            client, query, tile, api_key=api_key, timeout=timeout, gate=gate
        )

    with httpx.Client(timeout=timeout) as client:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as pool:
            futures = {pool.submit(search, client, task): task for task in tasks}
            for future in as_completed(futures):
                tile, query = futures[future]
                try:
                    found, calls = future.result()
                except Exception as exc:
                    errors.append(f"{query} at {tile[0]:.4f},{tile[1]:.4f}: {exc}")
                    continue
                calls_run += calls
                if len(found) >= _ID_SWEEP_MAX_PER_QUERY:
                    saturated += 1
                ids.update(found)

    return {
        "place_ids": sorted(ids),
        "id_count": len(ids),
        "tiles": len(tiles),
        "queries": len(queries),
        "calls_run": calls_run,
        "saturated_queries": saturated,
        "errors": errors,
    }


def fetch_place_summary(place_id: str, *, api_key: str, timeout: float = 30.0) -> dict:
    """Hydrate one unknown id — Pro-tier Details (5k free calls/month)."""
    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": SUMMARY_FIELD_MASK}
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(f"{PLACE_DETAILS_URL}{place_id}", headers=headers)
        resp.raise_for_status()
        data = resp.json()
    place = _normalize_place(data)
    place["business_status"] = data.get("businessStatus")
    return place


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
