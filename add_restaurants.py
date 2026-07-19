"""Add specific restaurants by name and run the pipeline for them.

Instead of (or on top of) area discovery, give a list of restaurant names.
Each name is resolved via Google Places Text Search (biased toward the
configured area, but an explicit "Antonio's Orlando" still wins), upserted
into the DB, then enriched (Google food signals), ingested (menu scrape),
and classified (Claude dish verdicts — ~$0.10/restaurant; skip with
--no-classify).

The resolved match (name + address) is always printed — spot-check it! A
wrong match here poisons everything downstream.

    python add_restaurants.py "4 Rivers Smokehouse" "Ethos Vegan Kitchen"
    python add_restaurants.py --file names.txt          # one name per line
    python add_restaurants.py --file names.txt --dry-run  # resolve only
    python add_restaurants.py "Some Place" --no-classify  # scrape only

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
from places_client import (
    RESOLVE_FIELD_MASK,
    _names_overlap,
    distance_meters,
    search_place_by_name,
    search_place_candidates,
)

# An open-data row and its Google match must agree on location this tightly;
# beyond it, Text Search has probably returned a same-name sibling elsewhere.
_RESOLVE_MAX_DISTANCE_METERS = 400.0


def resolve_external_place(place: dict) -> dict | None:
    """Match a non-Google prospect row (Overture sweep) to a Google place.

    Uses the Pro-tier field mask (no websiteUri — the open-data row already
    carries its website), so these calls stay inside the 5k/month free cap.
    A match must overlap on NAME (not address tokens) and, when both sides
    have coordinates, sit within _RESOLVE_MAX_DISTANCE_METERS. Returns the
    winning candidate or None — a wrong match here poisons everything
    downstream, so no confident match means don't add.
    """
    query = place["name"]
    if place.get("address"):
        query = f"{place['name']}, {place['address']}"
    candidates = search_place_candidates(
        query,
        api_key=settings.google_places_api_key,
        bias_lat=place.get("lat") if place.get("lat") is not None else settings.discovery_lat,
        bias_lng=place.get("lng") if place.get("lng") is not None else settings.discovery_lng,
        bias_radius_meters=5_000.0,
        field_mask=RESOLVE_FIELD_MASK,
    )
    for cand in candidates:
        if not _names_overlap(place["name"], cand["name"] or ""):
            continue
        if (
            place.get("lat") is not None
            and cand.get("lat") is not None
            and distance_meters(place["lat"], place["lng"], cand["lat"], cand["lng"])
            > _RESOLVE_MAX_DISTANCE_METERS
        ):
            continue
        return cand
    return None


def _resolve(name: str) -> dict | None:
    return search_place_by_name(
        name,
        api_key=settings.google_places_api_key,
        bias_lat=settings.discovery_lat,
        bias_lng=settings.discovery_lng,
    )


def resolve_candidates(names: list[str]) -> list[dict]:
    """Resolve names to selectable candidates — NO database writes.

    Powers the Admin add flow's confirm step: the user sees every plausible
    match (with weak name-overlap ones flagged) and picks the right place
    before anything is added. Existing place_ids are marked so re-adding is
    a visible choice (it refreshes, not duplicates).
    """
    if not settings.google_places_api_key:
        raise SystemExit("GOOGLE_PLACES_API_KEY not set in .env.")
    db.init_db()
    existing = {r["place_id"]: r["id"] for r in db.list_restaurants()}
    out: list[dict] = []
    for name in names:
        candidates = search_place_candidates(
            name,
            api_key=settings.google_places_api_key,
            bias_lat=settings.discovery_lat,
            bias_lng=settings.discovery_lng,
        )
        for cand in candidates:
            cand["already_added_id"] = existing.get(cand["place_id"])
        out.append({"query": name, "candidates": candidates})
    return out


def add_places(
    places: list[dict], *, do_ingest: bool = True, do_classify: bool = True
) -> dict:
    """Add user-CONFIRMED places, then run the chosen pipeline stages.

    Enrichment always runs (cheap, and Explore needs the food signals);
    menu scraping and classification are opt-in per the Admin add flow.
    """
    db.init_db()
    now = datetime.now(timezone.utc).isoformat()
    results: list[dict] = []
    for place in places:
        place = dict(place)
        from_overture = not place.get("place_id")
        if from_overture:
            # Open-data (Overture) row: resolve to a Google place first so
            # place_id-keyed dedupe and enrichment keep working. Google's
            # canonical name/address/location win; the open-data website URL
            # is kept (the resolve mask deliberately doesn't fetch one).
            resolved = resolve_external_place(place)
            if resolved is None:
                print(f"  [unresolved] {place.get('name')} — no confident Google match")
                results.append(
                    {
                        "id": None,
                        "name": place.get("name"),
                        "error": "No confident Google match; not added.",
                    }
                )
                continue
            resolved = dict(resolved)
            resolved["website_url"] = resolved.get("website_url") or place.get("website_url")
            resolved["discovery_source"] = "overture"
            place = resolved
        place["last_scraped_at"] = now
        db.upsert_restaurants([place])
        rid = next(
            r["id"] for r in db.list_restaurants() if r["place_id"] == place["place_id"]
        )
        entry = {"id": rid, "name": place.get("name"), "scraped": None, "dishes": None}
        if from_overture:
            # Details enrichment costs a metered Google call per restaurant;
            # bulk open-data adds defer it so the monthly free cap is spent
            # deliberately — the Admin "pending Google enrichment" panel (or
            # any normal enrich run) drains the queue.
            entry["enrich_deferred"] = True
        else:
            try:
                enrich.run(restaurant_id=rid)
            except Exception as exc:
                print(f"  enrich failed: {exc}")
        if do_ingest:
            try:
                summary = ingest.run(restaurant_id=rid)
                entry["scraped"] = summary["succeeded"] > 0
            except (SystemExit, Exception) as exc:
                print(f"  ingest failed: {exc}")
                entry["scraped"] = False
        if do_ingest and do_classify and entry["scraped"]:
            try:
                import classify

                summary = classify.run(restaurant_id=rid)
                entry["dishes"] = summary["dishes"]
            except (SystemExit, Exception) as exc:
                print(f"  classify failed: {exc}")
        results.append(entry)
    return {"added": results}


def run(names: list[str], dry_run: bool = False, classify_too: bool = True) -> dict:
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
            except SystemExit as exc:
                # No menu text (scrape failed) — nothing to classify.
                print(f"  classify skipped: {exc}")
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
    parser.add_argument("--no-classify", action="store_true",
                        help="Skip Claude dish classification (it runs by "
                        "default for each added restaurant; ~$0.10 each).")
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
    run(names, dry_run=args.dry_run, classify_too=not args.no_classify)


if __name__ == "__main__":
    main()
