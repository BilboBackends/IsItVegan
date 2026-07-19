"""Overture Maps open-data client for FREE radius food-venue sweeps.

The paid Google radius sweep tiles an area into Nearby Search cells and pays
per cell whether or not it finds anything new. Overture Maps publishes the
same kind of POI data (sourced from Meta, Microsoft, and Foursquare) as a
bulk-downloadable open dataset, so a discovery sweep over it costs nothing —
Google is only contacted later, when a chosen row is actually added to the
pipeline (add_restaurants resolves it to a real place_id then).

Rows come back in the same normalized shape as places_client sweeps, with
``place_id: None`` plus ``overture_id``/``confidence``/``source`` extras, so
the Admin Prospect panel can render them with the existing map/list/add flow.

Data freshness follows the Overture release pinned by the installed
``overturemaps`` package (monthly releases; ``pip install -U overturemaps``
to move forward). The S3 scan for a metro-sized bbox typically takes a
minute or two — slow, but free and unmetered.

Mock-first: pass ``rows=`` (an iterable of raw Overture place dicts) to skip
the network entirely; tests exercise filtering/normalization that way.
"""
from __future__ import annotations

import math
import struct

from places_client import _names_overlap, distance_meters

# Below this Overture confidence score a row is more likely noise (long-gone
# venue, bad conflation) than a real gap in our coverage.
DEFAULT_MIN_CONFIDENCE = 0.5

# Two accepted rows closer than this with overlapping names are treated as
# the same venue (Overture occasionally carries near-duplicate conflations).
_DUPLICATE_RADIUS_METERS = 100.0

# Same bounds the Google radius sweep enforces, for a consistent Admin UX.
_MIN_RADIUS_METERS = 250.0
_MAX_RADIUS_METERS = 50_000.0

_METERS_PER_DEG_LAT = 111_320.0

# Overture "eat_and_drink" primary categories we want in the pipeline.
# Allowlist (not blocklist): the Overture taxonomy is huge and includes
# groceries, venues, and services we must not sweep in. Mirrors the spirit of
# places_client.RADIUS_FOOD_TYPE_GROUPS — restaurants plus non-restaurant
# food venues, excluding drink-only bars/nightlife. Any category ending in
# "_restaurant" (mexican_restaurant, fast_food_restaurant, …) also counts.
FOOD_CATEGORIES = frozenset(
    {
        "restaurant", "acai_shop", "bagel_shop", "bakery", "bar_and_grill",
        "beer_garden", "bistro", "brewery", "brewpub", "bubble_tea_shop",
        "buffet", "cafe", "cafeteria", "cake_shop", "coffee_roastery",
        "coffee_shop", "creperie", "deli", "delicatessen", "dessert_shop",
        "diner", "donut_shop", "food_court", "food_stand", "food_truck",
        "frozen_yogurt_shop", "gastropub", "gelato_shop", "ice_cream_shop",
        "juice_bar", "juice_shop", "meal_takeaway", "pastry_shop", "pizzeria",
        "pub", "sandwich_shop", "smoothie_shop", "snack_bar", "sports_bar",
        "steakhouse", "tea_house", "tea_room", "winery",
    }
)


def _is_food_category(category: str | None) -> bool:
    if not category:
        return False
    return category in FOOD_CATEGORIES or category.endswith("_restaurant")


def _wkb_point(wkb: bytes | None) -> tuple[float, float] | None:
    """(lng, lat) from a WKB point, or None. Overture geometry is WKB."""
    if not wkb or len(wkb) < 21:
        return None
    order = "<" if wkb[0] == 1 else ">"
    (geom_type,) = struct.unpack_from(order + "I", wkb, 1)
    if geom_type & 0xFF != 1:  # not a point
        return None
    lng, lat = struct.unpack_from(order + "dd", wkb, 5)
    return lng, lat


def _format_address(addresses: list | None) -> str | None:
    if not addresses:
        return None
    addr = addresses[0] or {}
    street = addr.get("freeform")
    locality = addr.get("locality")
    region = addr.get("region")
    tail = " ".join(p for p in (region, addr.get("postcode")) if p)
    parts = [p for p in (street, locality, tail) if p]
    return ", ".join(parts) or None


def _bbox(lat: float, lng: float, radius_meters: float) -> tuple[float, float, float, float]:
    """(xmin, ymin, xmax, ymax) in lng/lat covering the sweep circle."""
    dlat = radius_meters / _METERS_PER_DEG_LAT
    dlng = radius_meters / (_METERS_PER_DEG_LAT * math.cos(math.radians(lat)))
    return (lng - dlng, lat - dlat, lng + dlng, lat + dlat)


def _stream_rows(bbox: tuple[float, float, float, float]):
    """Stream raw Overture place rows for a bbox from the public S3 dataset."""
    try:
        from overturemaps import core
    except ImportError as exc:  # dependency is in requirements.txt
        raise RuntimeError(
            "The 'overturemaps' package is not installed — run: "
            "pip install overturemaps"
        ) from exc
    reader = core.record_batch_reader("place", bbox)
    if reader is None:
        return
    try:
        for batch in reader:
            yield from batch.to_pylist()
    finally:
        reader.close()


def fetch_overture_food_places(
    lat: float,
    lng: float,
    radius_meters: float,
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    rows=None,
) -> dict:
    """Food venues within the circle, from Overture open data. Zero API cost.

    Returns the same envelope shape as radius_food_sweep: {"places", "count",
    ...} plus drop counters so the Admin notice can say why rows were culled.
    """
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise ValueError("Sweep center is outside valid latitude/longitude bounds.")
    if not _MIN_RADIUS_METERS <= radius_meters <= _MAX_RADIUS_METERS:
        raise ValueError("Sweep radius must be between 250 m and 50 km.")
    if rows is None:
        rows = _stream_rows(_bbox(lat, lng, radius_meters))

    candidates: list[dict] = []
    scanned = 0
    dropped_non_food = 0
    dropped_low_confidence = 0
    dropped_closed = 0
    for row in rows:
        scanned += 1
        category = (row.get("categories") or {}).get("primary")
        if not _is_food_category(category):
            dropped_non_food += 1
            continue
        confidence = row.get("confidence")
        if confidence is not None and confidence < min_confidence:
            dropped_low_confidence += 1
            continue
        status = row.get("operating_status")
        if status and status != "open":
            dropped_closed += 1
            continue
        point = _wkb_point(row.get("geometry"))
        name = (row.get("names") or {}).get("primary")
        if point is None or not name:
            continue
        p_lng, p_lat = point
        distance = distance_meters(lat, lng, p_lat, p_lng)
        if distance > radius_meters:
            continue
        websites = row.get("websites") or []
        candidates.append(
            {
                "place_id": None,
                "overture_id": row.get("id"),
                "name": name,
                "address": _format_address(row.get("addresses")),
                "website_url": websites[0] if websites else None,
                "lat": p_lat,
                "lng": p_lng,
                "primary_type": category,
                "confidence": round(confidence, 2) if confidence is not None else None,
                "distance_meters": round(distance),
                "source": "overture",
            }
        )

    # Collapse near-duplicate conflations, keeping the most confident row.
    candidates.sort(key=lambda p: -(p["confidence"] or 0.0))
    accepted: list[dict] = []
    for cand in candidates:
        duplicate = any(
            _names_overlap(cand["name"], kept["name"])
            and distance_meters(cand["lat"], cand["lng"], kept["lat"], kept["lng"])
            <= _DUPLICATE_RADIUS_METERS
            for kept in accepted
        )
        if not duplicate:
            accepted.append(cand)

    accepted.sort(
        key=lambda p: (p.get("distance_meters", math.inf), p["name"].lower())
    )
    return {
        "places": accepted,
        "count": len(accepted),
        "source": "overture",
        "scanned": scanned,
        "dropped_non_food": dropped_non_food,
        "dropped_low_confidence": dropped_low_confidence,
        "dropped_closed": dropped_closed,
        "min_confidence": min_confidence,
    }


def mark_known_places(
    places: list[dict],
    existing_restaurants: list[dict],
    *,
    max_distance_meters: float = 150.0,
) -> int:
    """Flag rows already in the pipeline; return the still-new count.

    Overture rows carry no Google place_id, so the place_id lookup the other
    prospect endpoints use can't work — instead a row matches an existing
    restaurant when it sits within max_distance_meters AND the names overlap
    (both guards together keep a food hall's neighbors distinct).
    """
    located = [
        r
        for r in existing_restaurants
        if r.get("lat") is not None and r.get("lng") is not None
    ]
    new_count = 0
    for place in places:
        known = None
        for rest in located:
            if (
                distance_meters(place["lat"], place["lng"], rest["lat"], rest["lng"])
                <= max_distance_meters
                and _names_overlap(place["name"], rest["name"] or "")
            ):
                known = rest
                break
        place["already_added_id"] = known["id"] if known else None
        place["archived"] = bool(known and known.get("archived"))
        if not known:
            new_count += 1
    return new_count
