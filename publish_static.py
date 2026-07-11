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
from venue_filter import is_consumer_food_venue

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


def export() -> dict:
    db.init_db()
    counts = db.verdict_counts_by_restaurant()
    restaurants = []
    for r in db.list_restaurants():
        if not is_consumer_food_venue(r):
            continue
        row = {field: r.get(field) for field in _RESTAURANT_FIELDS}
        c = counts.get(r["id"])
        row["dish_count"] = c["total"] if c else 0
        row["vegan_options"] = c["vegan_meals"] if c else 0
        row["vegan_sides"] = c["vegan_sides"] if c else 0
        menu_source = db.get_menu_text(r["id"])
        score = compute_vegan_score(
            vegan_meals=c["vegan_meals"] if c else 0,
            vegan_sides=c["vegan_sides"] if c else 0,
            substance_points=c.get("vegan_substance_points", 0.0) if c else 0.0,
            google_rating=r.get("rating"),
            dessert_venue=r.get("primary_type") in db.DESSERT_VENUE_TYPES,
            plant_protein_menu=menu_offers_plant_protein(
                menu_source["content"] if menu_source else None
            ),
        )
        row["vegan_score"] = score["score"]
        row["vegan_score_parts"] = score
        restaurants.append(row)

    dishes = [d for d in db.list_all_dishes() if is_consumer_food_venue(d)]

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
