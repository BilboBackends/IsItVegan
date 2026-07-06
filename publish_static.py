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
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db
from venue_filter import is_consumer_food_venue

DATA_DIR = Path(__file__).parent / "frontend" / "public" / "data"

# The consumer UI reads exactly these restaurant fields — admin bookkeeping
# (costs, hashes, refresh/crawl state) stays local.
_RESTAURANT_FIELDS = (
    # place_id is public Google data and the frontend's marker/focus key —
    # without it every card matched the "focused" check (undefined ===
    # undefined) and map pins collided on one key.
    "id", "name", "address", "place_id", "website_url", "lat", "lng",
    "primary_type", "price_level", "serves_vegetarian", "editorial_summary",
    "rating", "user_rating_count", "open_now", "opening_hours",
    "enriched_at", "menu_fetched_at",
)


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
        restaurants.append(row)

    dishes = [d for d in db.list_all_dishes() if is_consumer_food_venue(d)]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    published_at = datetime.now(timezone.utc).isoformat()
    (DATA_DIR / "restaurants.json").write_text(
        json.dumps(
            {"count": len(restaurants), "restaurants": restaurants,
             "published_at": published_at},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (DATA_DIR / "dishes.json").write_text(
        json.dumps(
            {"count": len(dishes), "dishes": dishes,
             "published_at": published_at},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {"restaurants": len(restaurants), "dishes": len(dishes)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish static site data.")
    parser.add_argument(
        "--push", action="store_true",
        help="Commit the exported data and push (triggers the Pages deploy).",
    )
    args = parser.parse_args()

    summary = export()
    print(
        f"Exported {summary['restaurants']} restaurants and "
        f"{summary['dishes']} dishes to {DATA_DIR}"
    )
    if args.push:
        subprocess.run(["git", "add", str(DATA_DIR)], check=True)
        result = subprocess.run(
            ["git", "commit", "-m", "Publish site data"], capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print("Nothing new to publish (data unchanged).")
            return
        subprocess.run(["git", "push"], check=True)
        print("Pushed — the Pages deploy will pick it up.")


if __name__ == "__main__":
    main()
