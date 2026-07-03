"""Enrichment: pull Google's structured food signals per restaurant.

Google Places (New) Place Details returns food-related fields for many
restaurants — servesVegetarianFood, priceLevel, primaryType, and a short
editorialSummary that often names signature dishes. These are free-ish
(a few fields on a details call) and, crucially, available even for
restaurants whose own website we couldn't scrape.

We store the booleans/blurb on the restaurant row, and also persist the
editorial summary as a restaurant-level 'text' source so it becomes evidence
the classifier can cite (like scraped menu text).

Runnable in isolation (per CLAUDE.md conventions):

    python enrich.py                    # enrich restaurants not yet enriched
    python enrich.py --all              # re-fetch for everyone
    python enrich.py --restaurant-id 30 # just one
    python enrich.py --dry-run          # report, don't write
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db
from config import settings
from places_client import fetch_place_details

# Where the editorial-summary evidence source is recorded as coming from.
_EDITORIAL_URL = "google:editorial_summary"


def _targets(restaurant_id: int | None, do_all: bool) -> list[dict]:
    rows = db.list_restaurants()
    if restaurant_id is not None:
        rows = [r for r in rows if r["id"] == restaurant_id]
        if not rows:
            raise SystemExit(f"No restaurant with id {restaurant_id}.")
        return rows
    if do_all:
        return rows
    return [r for r in rows if not r.get("enriched_at")]


def run(
    restaurant_id: int | None = None,
    do_all: bool = False,
    dry_run: bool = False,
) -> dict:
    if not settings.google_places_api_key:
        raise SystemExit("GOOGLE_PLACES_API_KEY not set in .env.")

    db.init_db()
    targets = _targets(restaurant_id, do_all)
    print(f"Enriching {len(targets)} restaurant(s)...\n")

    now = datetime.now(timezone.utc).isoformat()
    veg_yes = veg_no = veg_unknown = with_editorial = 0

    for t in targets:
        details = fetch_place_details(
            t["place_id"], api_key=settings.google_places_api_key
        )
        veg = details["serves_vegetarian"]
        veg_str = {True: "veg✓", False: "veg✗", None: "veg?"}[veg]
        if veg is True:
            veg_yes += 1
        elif veg is False:
            veg_no += 1
        else:
            veg_unknown += 1

        editorial = details["editorial_summary"]
        if editorial:
            with_editorial += 1

        print(f"  [{veg_str}] {t['name']}  ({details['primary_type'] or '—'})")
        if editorial:
            print(f"          “{editorial[:70]}”")

        if not dry_run:
            db.update_food_signals(
                t["id"],
                serves_vegetarian=veg,
                price_level=details["price_level"],
                primary_type=details["primary_type"],
                editorial_summary=editorial,
                enriched_at=now,
            )
            # Persist the blurb as citable evidence alongside scraped menu text.
            if editorial:
                db.upsert_menu_text(
                    restaurant_id=t["id"],
                    url=_EDITORIAL_URL,
                    content=f"Google editorial summary: {editorial}",
                    fetched_at=now,
                )

    print(
        f"\nDone. vegetarian: {veg_yes} yes / {veg_no} no / {veg_unknown} unknown. "
        f"{with_editorial} have an editorial summary."
    )
    if dry_run:
        print("[dry-run] Nothing written to the database.")
    return {
        "veg_yes": veg_yes,
        "veg_no": veg_no,
        "veg_unknown": veg_unknown,
        "with_editorial": with_editorial,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Google food-signal enrichment.")
    parser.add_argument("--restaurant-id", type=int, default=None)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(restaurant_id=args.restaurant_id, do_all=args.all, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
