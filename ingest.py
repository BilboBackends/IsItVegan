"""Phase 1: menu-text ingestion.

Scrapes each restaurant's website for readable menu text and stores it as a
restaurant-level 'text' source. Idempotent — re-running upserts on
(restaurant_id, url), so it can be scheduled without duplicating.

Runnable in isolation (per CLAUDE.md conventions):

    # Ingest every restaurant with a website that has no menu text yet:
    python ingest.py

    # Re-scrape everything, even those already ingested:
    python ingest.py --all

    # Just one restaurant by DB id (great for debugging a single site):
    python ingest.py --restaurant-id 12

    # Don't write to the DB, just report what would happen:
    python ingest.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

# Restaurant names / menu text contain non-ASCII; force UTF-8 stdout so the
# Windows cp1252 console doesn't crash mid-run (same fix as discover.py).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db
from scraper import scrape_menu_text


def _targets(restaurant_id: int | None, do_all: bool) -> list[dict]:
    """Pick which restaurants to ingest."""
    if restaurant_id is not None:
        rows = [r for r in db.list_restaurants() if r["id"] == restaurant_id]
        if not rows:
            raise SystemExit(f"No restaurant with id {restaurant_id}.")
        if not rows[0].get("website_url"):
            raise SystemExit(f"Restaurant {restaurant_id} has no website_url.")
        return [{"id": rows[0]["id"], "name": rows[0]["name"],
                 "website_url": rows[0]["website_url"]}]
    if do_all:
        return [
            {"id": r["id"], "name": r["name"], "website_url": r["website_url"]}
            for r in db.list_restaurants()
            if r.get("website_url")
        ]
    return db.restaurants_needing_ingest()


def run(
    restaurant_id: int | None = None,
    do_all: bool = False,
    dry_run: bool = False,
) -> dict:
    db.init_db()
    targets = _targets(restaurant_id, do_all)

    print(f"Ingesting {len(targets)} restaurant(s)...\n")
    succeeded, failed = 0, 0
    failures: list[tuple[str, str]] = []
    now = datetime.now(timezone.utc).isoformat()

    for t in targets:
        result = scrape_menu_text(t["website_url"])
        if result.ok:
            succeeded += 1
            print(f"  [ok]   {t['name']}  ({result.char_count} chars)")
            if not dry_run:
                db.upsert_menu_text(
                    restaurant_id=t["id"],
                    url=result.url,
                    content=result.text,
                    fetched_at=now,
                )
        else:
            failed += 1
            failures.append((t["name"], result.error or "unknown error"))
            print(f"  [fail] {t['name']}  — {result.error}")

    print(f"\nDone. {succeeded} succeeded, {failed} failed.")
    if failures:
        print("Failures (candidates for photo fallback / manual review):")
        for name, err in failures:
            print(f"  - {name}: {err}")
    if dry_run:
        print("\n[dry-run] Nothing written to the database.")

    return {"succeeded": succeeded, "failed": failed, "failures": failures}


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1 menu-text ingestion.")
    parser.add_argument(
        "--restaurant-id", type=int, default=None,
        help="Ingest only this restaurant (by DB id).",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Re-scrape all restaurants with a website, even if already ingested.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report results without writing to the database.",
    )
    args = parser.parse_args()
    run(restaurant_id=args.restaurant_id, do_all=args.all, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
