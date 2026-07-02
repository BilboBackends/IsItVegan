"""Phase 0: restaurant discovery.

Pulls restaurants near the configured center (Maitland, FL by default) from
the Google Places API and upserts them into SQLite. Idempotent — re-running
updates existing rows (keyed on place_id) rather than duplicating.

Runnable in isolation (per CLAUDE.md coding conventions):

    # Live (requires GOOGLE_PLACES_API_KEY in .env):
    python discover.py

    # Mock (no key / no network):
    python discover.py --mock fixtures/maitland_sample.json

    # Just print what would be persisted, don't write to the DB:
    python discover.py --mock fixtures/maitland_sample.json --dry-run
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

# Restaurant names contain non-ASCII characters (curly quotes, accents). The
# Windows console defaults to cp1252 and crashes on them, so force UTF-8 and
# replace anything unencodable rather than aborting a whole discovery run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db
from config import settings
from places_client import discover_restaurants


def run(mock_fixture_path: str | None = None, dry_run: bool = False) -> list[dict]:
    result = discover_restaurants(
        api_key=settings.google_places_api_key,
        lat=settings.discovery_lat,
        lng=settings.discovery_lng,
        radius_meters=settings.discovery_radius_meters,
        cell_radius_meters=settings.discovery_cell_radius_meters,
        city_filter=settings.discovery_city,
        mock_fixture_path=mock_fixture_path,
    )
    restaurants = result["restaurants"]

    now = datetime.now(timezone.utc).isoformat()
    for r in restaurants:
        r["last_scraped_at"] = now

    if settings.discovery_city:
        print(
            f"Found {result['raw_count']} in search area; "
            f"dropped {result['dropped']} outside {settings.discovery_city}."
        )
    print(f"Discovered {len(restaurants)} restaurant(s):")
    for r in restaurants:
        site = r.get("website_url") or "(no website)"
        print(f"  - {r['name']}  |  {r.get('address')}  |  {site}")

    if dry_run:
        print("\n[dry-run] Nothing written to the database.")
        return restaurants

    db.init_db()
    db.upsert_restaurants(restaurants)
    total = db.count_restaurants()
    print(f"\nPersisted to {settings.database_path}. Total restaurants in DB: {total}")
    return restaurants


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 0 restaurant discovery.")
    parser.add_argument(
        "--mock",
        dest="mock",
        default=None,
        help="Path to a JSON fixture of Places results (skips the live API).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results without writing to the database.",
    )
    args = parser.parse_args()
    run(mock_fixture_path=args.mock, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
