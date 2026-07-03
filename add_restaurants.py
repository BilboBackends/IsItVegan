"""Add specific restaurants by name and run the pipeline for them.

Instead of (or on top of) area discovery, give a list of restaurant names.
Each name is resolved via Google Places Text Search (biased toward the
configured area, but an explicit "Antonio's Orlando" still wins), upserted
into the DB, then enriched (Google food signals) and ingested (menu scrape).
Classification is opt-in via --classify since it spends Claude credits.

The resolved match (name + address) is always printed — spot-check it! A
wrong match here poisons everything downstream.

    python add_restaurants.py "4 Rivers Smokehouse" "Ethos Vegan Kitchen"
    python add_restaurants.py --file names.txt          # one name per line
    python add_restaurants.py --file names.txt --dry-run  # resolve only
    python add_restaurants.py "Some Place" --classify   # incl. Claude verdicts

No city filter is applied: an explicitly named restaurant is explicit intent.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db
import enrich
import ingest
from config import settings
from places_client import search_place_by_name


def _resolve(name: str) -> dict | None:
    return search_place_by_name(
        name,
        api_key=settings.google_places_api_key,
        bias_lat=settings.discovery_lat,
        bias_lng=settings.discovery_lng,
    )


def run(names: list[str], dry_run: bool = False, classify_too: bool = False) -> dict:
    if not settings.google_places_api_key:
        raise SystemExit("GOOGLE_PLACES_API_KEY not set in .env.")
    db.init_db()

    now = datetime.now(timezone.utc).isoformat()
    added: list[int] = []
    matches: list[dict] = []
    not_found: list[str] = []

    print(f"Resolving {len(names)} name(s) via Places Text Search...\n")
    for name in names:
        place = _resolve(name)
        if place is None:
            not_found.append(name)
            print(f"  [not found] {name}")
            continue
        print(f"  [match] {name!r} -> {place['name']}")
        print(f"          {place.get('address')}")
        matches.append(
            {"query": name, "matched": place["name"], "address": place.get("address")}
        )
        if dry_run:
            continue
        place["last_scraped_at"] = now
        db.upsert_restaurants([place])
        rid = next(
            r["id"] for r in db.list_restaurants() if r["place_id"] == place["place_id"]
        )
        added.append(rid)

    if dry_run:
        print("\n[dry-run] Nothing written; matches shown above.")
        return {"added": [], "matches": matches, "not_found": not_found}

    for rid in added:
        print(f"\n--- pipeline for restaurant id {rid} ---")
        try:
            enrich.run(restaurant_id=rid)
        except Exception as exc:
            print(f"  enrich failed: {exc}")
        try:
            ingest.run(restaurant_id=rid)
        except SystemExit as exc:
            print(f"  ingest skipped: {exc}")
        except Exception as exc:
            print(f"  ingest failed: {exc}")
        if classify_too:
            try:
                import classify

                classify.run(restaurant_id=rid)
            except Exception as exc:
                print(f"  classify failed: {exc}")

    print(
        f"\nDone. {len(added)} restaurant(s) through the pipeline, "
        f"{len(not_found)} not found."
    )
    return {"added": added, "matches": matches, "not_found": not_found}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add restaurants by name and run the pipeline for them."
    )
    parser.add_argument("names", nargs="*", help="Restaurant names.")
    parser.add_argument("--file", default=None, help="File with one name per line.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve and show matches only; write nothing.")
    parser.add_argument("--classify", action="store_true",
                        help="Also run Claude dish classification (costs credits).")
    args = parser.parse_args()

    names = list(args.names)
    if args.file:
        names += [
            line.strip()
            for line in Path(args.file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    if not names:
        raise SystemExit("Give names as arguments or via --file.")
    run(names, dry_run=args.dry_run, classify_too=args.classify)


if __name__ == "__main__":
    main()
