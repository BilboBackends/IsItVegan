"""Export consumer data as static JSON for the public site (GitHub Pages).

The public site has no backend: it serves the built frontend plus the JSON
snapshots this script writes to frontend/public/data/. Publishing flow:

    python publish_static.py            # export from the local SQLite DB
    python publish_static.py --push     # export + commit + push (deploys)

Only consumer-facing data ships: archived/hidden/non-food venues are
excluded (same venue_filter gate as the live consumer API), and admin-only
fields (costs, hashes, crawl bookkeeping) are stripped. No keys, no menus'
raw source text, no pipeline state.
"""
from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db
from vegan_score import compute_vegan_score, menu_offers_plant_protein
from venue_filter import is_consumer_food_venue, is_consumer_ready

DATA_DIR = Path(__file__).parent / "frontend" / "public" / "data"
RESTAURANT_DISH_DIR = DATA_DIR / "restaurant-dishes"

# The consumer UI reads exactly these restaurant fields — admin bookkeeping
# (costs, hashes, refresh/crawl state) stays local.
_RESTAURANT_FIELDS = (
    # place_id is public Google data and the frontend's marker/focus key —
    # without it every card matched the "focused" check (undefined ===
    # undefined) and map pins collided on one key.
    "id", "name", "address", "place_id", "website_url", "lat", "lng",
    "primary_type", "price_level", "serves_vegetarian", "editorial_summary",
    "rating", "user_rating_count", "open_now", "opening_hours",
    "enriched_at", "menu_fetched_at", "business_status",
)


def _write_restaurant_dish_shards(
    dishes: list[dict], published_at: str
) -> None:
    """Write one consumer menu file per restaurant and remove stale shards."""
    RESTAURANT_DISH_DIR.mkdir(parents=True, exist_ok=True)
    dishes_by_restaurant: dict[int, list[dict]] = {}
    for dish in dishes:
        dishes_by_restaurant.setdefault(int(dish["restaurant_id"]), []).append(dish)

    shard_names = set()
    for restaurant_id, restaurant_dishes in dishes_by_restaurant.items():
        shard_name = f"{restaurant_id}.json"
        shard_names.add(shard_name)
        (RESTAURANT_DISH_DIR / shard_name).write_text(
            json.dumps(
                {
                    "count": len(restaurant_dishes),
                    "dishes": restaurant_dishes,
                    "published_at": published_at,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    for stale_path in RESTAURANT_DISH_DIR.glob("*.json"):
        if stale_path.name not in shard_names:
            stale_path.unlink()


def _write_restaurant_dish_shard(
    restaurant_id: int, dishes: list[dict], published_at: str
) -> None:
    """Update one existing consumer menu without touching other shards."""
    RESTAURANT_DISH_DIR.mkdir(parents=True, exist_ok=True)
    _write_json_snapshot(
        RESTAURANT_DISH_DIR / f"{restaurant_id}.json",
        {"count": len(dishes), "dishes": dishes, "published_at": published_at},
    )


def _json_bytes(payload: dict) -> bytes:
    """Compact UTF-8 JSON shared by plain and pre-compressed snapshots."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_gzip_snapshot(path: Path, payload: dict) -> None:
    """Write a deterministic pre-compressed JSON snapshot."""
    path.write_bytes(gzip.compress(_json_bytes(payload), compresslevel=9, mtime=0))


def _write_json_snapshot(path: Path, payload: dict, *, gzip_copy: bool = False) -> None:
    """Write a compact snapshot and, when requested, a deterministic .gz copy."""
    content = _json_bytes(payload)
    path.write_bytes(content)
    if gzip_copy:
        _write_gzip_snapshot(path.with_suffix(path.suffix + ".gz"), payload)


def _consumer_restaurant_row(restaurant: dict, counts: dict | None) -> dict:
    row = {field: restaurant.get(field) for field in _RESTAURANT_FIELDS}
    row["dish_count"] = counts["total"] if counts else 0
    row["vegan_options"] = counts["vegan_meals"] if counts else 0
    row["vegan_sides"] = counts["vegan_sides"] if counts else 0
    menu_source = db.get_menu_text(restaurant["id"])
    score = compute_vegan_score(
        vegan_meals=counts["vegan_meals"] if counts else 0,
        vegan_sides=counts["vegan_sides"] if counts else 0,
        substance_points=counts.get("vegan_substance_points", 0.0) if counts else 0.0,
        google_rating=restaurant.get("rating"),
        dessert_venue=restaurant.get("primary_type") in db.DESSERT_VENUE_TYPES,
        plant_protein_menu=menu_offers_plant_protein(
            menu_source["content"] if menu_source else None
        ),
    )
    row["vegan_score"] = score["score"]
    row["vegan_score_parts"] = score
    return row


def export() -> dict:
    db.init_db()
    counts = db.verdict_counts_by_restaurant()
    restaurants = []
    for r in db.list_restaurants():
        c = counts.get(r["id"])
        if not is_consumer_ready(r, c["total"] if c else 0):
            continue
        restaurants.append(_consumer_restaurant_row(r, c))

    dishes = [
        d
        for d in db.list_all_dishes()
        if is_consumer_food_venue(d) and d.get("verdict")
    ]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    published_at = datetime.now(timezone.utc).isoformat()
    _write_json_snapshot(
        DATA_DIR / "restaurants.json",
        {"count": len(restaurants), "restaurants": restaurants,
         "published_at": published_at},
    )
    _write_json_snapshot(
        DATA_DIR / "dishes.json",
        {"count": len(dishes), "dishes": dishes,
         "published_at": published_at},
        gzip_copy=True,
    )

    # Restaurant cards and map popups open one menu at a time. Small shards
    # keep that common action from downloading the full cross-menu search
    # index (currently tens of megabytes on the public site).
    _write_restaurant_dish_shards(dishes, published_at)
    return {"restaurants": len(restaurants), "dishes": len(dishes)}


def export_restaurant(restaurant_id: int) -> dict:
    """Refresh one already-published restaurant without exporting local backlog.

    Deep-dive repairs often need to reach production immediately while other
    newly scraped restaurants are still awaiting review. This preserves the
    published restaurant set and replaces only the selected row and dishes.
    """
    db.init_db()
    restaurants_path = DATA_DIR / "restaurants.json"
    dishes_path = DATA_DIR / "dishes.json"
    if not restaurants_path.exists() or not dishes_path.exists():
        raise RuntimeError("Static snapshots do not exist; run a full export first.")

    restaurant_snapshot = json.loads(restaurants_path.read_text(encoding="utf-8"))
    dish_snapshot = json.loads(dishes_path.read_text(encoding="utf-8"))
    published_rows = restaurant_snapshot.get("restaurants") or []
    if not any(row.get("id") == restaurant_id for row in published_rows):
        raise RuntimeError(
            f"Restaurant {restaurant_id} is not already published; use a full export."
        )

    restaurant = next(
        (row for row in db.list_restaurants() if row["id"] == restaurant_id),
        None,
    )
    if restaurant is None:
        raise RuntimeError(f"Restaurant {restaurant_id} was not found locally.")
    counts = db.verdict_counts_by_restaurant().get(restaurant_id)
    if not is_consumer_ready(restaurant, counts["total"] if counts else 0):
        raise RuntimeError(f"Restaurant {restaurant_id} is not consumer-ready.")

    replacement = _consumer_restaurant_row(restaurant, counts)
    published_rows = [
        replacement if row.get("id") == restaurant_id else row
        for row in published_rows
    ]
    target_dishes = [
        dish
        for dish in db.list_all_dishes()
        if dish.get("restaurant_id") == restaurant_id
        and is_consumer_food_venue(dish)
        and dish.get("verdict")
    ]
    previous_dishes = dish_snapshot.get("dishes") or []
    target_indexes = [
        index
        for index, dish in enumerate(previous_dishes)
        if dish.get("restaurant_id") == restaurant_id
    ]
    insert_at = target_indexes[0] if target_indexes else len(previous_dishes)
    published_dishes = [
        dish
        for dish in previous_dishes
        if dish.get("restaurant_id") != restaurant_id
    ]
    target_dishes.sort(
        key=lambda dish: (
            str(dish.get("name") or "").casefold(),
            str(dish.get("restaurant_name") or "").casefold(),
        )
    )
    published_dishes[insert_at:insert_at] = target_dishes

    published_at = datetime.now(timezone.utc).isoformat()
    _write_json_snapshot(
        restaurants_path,
        {
            "count": len(published_rows),
            "restaurants": published_rows,
            "published_at": published_at,
        },
    )
    _write_json_snapshot(
        dishes_path,
        {
            "count": len(published_dishes),
            "dishes": published_dishes,
            "published_at": published_at,
        },
        gzip_copy=True,
    )
    _write_restaurant_dish_shard(restaurant_id, target_dishes, published_at)
    return {
        "restaurants": len(published_rows),
        "dishes": len(published_dishes),
        "restaurant_id": restaurant_id,
        "restaurant_dishes": len(target_dishes),
    }


def publish(push: bool = False) -> dict:
    """Export snapshots; optionally commit + push them (Pages redeploys).

    Only the data directory is staged, so uncommitted code changes are never
    swept into a publish. Callable from the CLI and the Admin publish button
    (POST /api/publish). Raises RuntimeError when the push itself fails.
    """
    summary = export()
    summary["pushed"] = False
    summary["message"] = (
        f"Exported {summary['restaurants']} restaurants and "
        f"{summary['dishes']} dishes."
    )
    if not push:
        return summary

    root = Path(__file__).parent
    subprocess.run(
        ["git", "add", str(DATA_DIR)],
        cwd=root, capture_output=True, text=True, check=True,
    )
    commit = subprocess.run(
        ["git", "commit", "-m", "Publish site data"],
        cwd=root, capture_output=True, text=True,
    )
    if commit.returncode != 0:
        summary["message"] = "Live site already up to date — data unchanged."
        return summary
    pushed = subprocess.run(
        ["git", "push"], cwd=root, capture_output=True, text=True, timeout=180,
    )
    if pushed.returncode != 0:
        raise RuntimeError(
            "git push failed: "
            + ((pushed.stderr or pushed.stdout or "").strip()[-400:])
        )
    summary["pushed"] = True
    summary["message"] = (
        f"Published {summary['restaurants']} restaurants / "
        f"{summary['dishes']} dishes — the live site redeploys in ~2 minutes."
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish static site data.")
    parser.add_argument(
        "--push", action="store_true",
        help="Commit the exported data and push (triggers the Pages deploy).",
    )
    args = parser.parse_args()
    summary = publish(push=args.push)
    print(summary["message"])


if __name__ == "__main__":
    main()
