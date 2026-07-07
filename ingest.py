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
from venue_filter import is_consumer_food_venue


def _targets(
    restaurant_id: int | None,
    do_all: bool,
    stale_days: int | None = None,
    restaurant_ids: list[int] | None = None,
) -> list[dict]:
    """Pick which restaurants to ingest.

    Bulk modes skip venues excluded from consumer views (gas stations,
    convenience stores like 7-Eleven, manually hidden listings) — scraping
    them produced junk "menus" from generic chain sites. An explicit
    --restaurant-id still ingests anything, for debugging.
    """
    all_rows = db.list_restaurants()
    if restaurant_id is not None:
        rows = [r for r in all_rows if r["id"] == restaurant_id]
        if not rows:
            raise SystemExit(f"No restaurant with id {restaurant_id}.")
        if not rows[0].get("website_url"):
            raise SystemExit(f"Restaurant {restaurant_id} has no website_url.")
        return [{"id": rows[0]["id"], "name": rows[0]["name"],
                 "website_url": rows[0]["website_url"]}]

    eligible = {
        r["id"]
        for r in all_rows
        if is_consumer_food_venue(r) and r.get("refresh_enabled", 1)
    }
    if restaurant_ids is not None:
        requested = set(restaurant_ids)
        targets = [
            {"id": r["id"], "name": r["name"], "website_url": r["website_url"]}
            for r in all_rows
            if r["id"] in requested and r.get("website_url")
        ]
    elif do_all:
        targets = [
            {"id": r["id"], "name": r["name"], "website_url": r["website_url"]}
            for r in all_rows
            if r.get("website_url")
        ]
    elif stale_days is not None:
        targets = db.restaurants_needing_refresh(stale_days)
    else:
        targets = db.restaurants_needing_ingest()
    skipped = [t for t in targets if t["id"] not in eligible]
    if skipped:
        print(f"Skipping {len(skipped)} non-consumer venue(s): "
              + ", ".join(t["name"] for t in skipped))
    return [t for t in targets if t["id"] in eligible]


def run(
    restaurant_id: int | None = None,
    do_all: bool = False,
    dry_run: bool = False,
    stale_days: int | None = None,
    restaurant_ids: list[int] | None = None,
    on_progress=None,
    should_stop=None,
) -> dict:
    """Scrape targets; on_progress (optional) receives event dicts so a live
    caller (the Admin dashboard) can show progress: {"total": N} once targets
    are known, {"current": name} before each scrape, {"result": {...}} after.

    should_stop (optional, callable -> bool) is checked before each
    restaurant; when it returns True the loop exits cleanly, keeping every
    menu scraped so far. It cannot interrupt a scrape already in flight — a
    hung headless browser is unstuck separately by killing its processes.
    """
    def _emit(event: dict) -> None:
        if on_progress is not None:
            on_progress(event)

    db.init_db()
    targets = _targets(restaurant_id, do_all, stale_days, restaurant_ids)
    _emit({"total": len(targets)})

    print(f"Ingesting {len(targets)} restaurant(s)...\n")
    succeeded, failed = 0, 0
    failures: list[tuple[str, str]] = []
    cancelled = False
    now = datetime.now(timezone.utc).isoformat()

    for t in targets:
        if should_stop is not None and should_stop():
            cancelled = True
            print("Stop requested — halting after completed restaurants.")
            break
        _emit({"current": t["name"]})
        crawl_profile = db.get_crawl_profile(t["id"])
        result = scrape_menu_text(
            t["website_url"], crawl_context=crawl_profile
        )
        if result.ok:
            succeeded += 1
            pages = result.pages or [(result.menu_url or result.url, result.text)]
            changed = (
                not crawl_profile
                or not crawl_profile.get("content_hash")
                or crawl_profile.get("content_hash") != result.content_hash
            )
            route = "learned route" if result.used_learned_context else "discovery"
            structured = (
                f", {result.structured_item_count} structured items"
                if result.structured_item_count
                else ""
            )
            print(
                f"  [menu] {t['name']}  "
                f"(score {result.menu_score:.2f}, {len(pages)} page(s), "
                f"{result.char_count} chars{structured}, {result.crawl_method}/{route}, "
                f"{'changed' if changed else 'unchanged'})"
            )
            if not dry_run:
                # One source row per kept page; stale pages from earlier
                # scrapes are pruned so classification sees the current menu.
                db.replace_menu_texts(t["id"], pages, fetched_at=now)
                # Immutable version history: only distinct content adds a
                # row, so the table reads as "every time the menu changed".
                if result.content_hash:
                    db.record_menu_version(
                        t["id"],
                        result.text,
                        result.content_hash,
                        menu_score=result.menu_score,
                        char_count=result.char_count,
                        fetched_at=now,
                    )
                db.record_crawl_success(
                    t["id"],
                    menu_urls=[page_url for page_url, _ in pages],
                    crawl_method=result.crawl_method or "http",
                    content_hash=result.content_hash or "",
                    menu_score=result.menu_score,
                    char_count=result.char_count,
                    crawled_at=now,
                )
            _emit({"result": {
                "name": t["name"], "ok": True,
                "pages": len(pages), "chars": result.char_count,
                "score": result.menu_score,
                "method": result.crawl_method,
                "learned_route": result.used_learned_context,
                "changed": changed,
                "structured_items": result.structured_item_count,
                "structured_categories": result.structured_category_count,
                "diagnostics": result.diagnostics,
            }})
        else:
            failed += 1
            failures.append((t["name"], result.error or "unknown error"))
            if not dry_run:
                db.record_crawl_failure(
                    t["id"], result.error or "unknown error", attempted_at=now
                )
            print(f"  [fail] {t['name']}  — {result.error}")
            _emit({"result": {
                "name": t["name"], "ok": False, "error": result.error,
            }})

    print(f"\nDone. {succeeded} succeeded, {failed} failed.")
    if failures:
        print("Failures (candidates for photo fallback / manual review):")
        for name, err in failures:
            print(f"  - {name}: {err}")
    if dry_run:
        print("\n[dry-run] Nothing written to the database.")

    return {
        "succeeded": succeeded,
        "failed": failed,
        "failures": failures,
        "cancelled": cancelled,
    }


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
        "--stale-days", type=int, default=None,
        help="Re-scrape menus older than this many days.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report results without writing to the database.",
    )
    args = parser.parse_args()
    run(
        restaurant_id=args.restaurant_id,
        do_all=args.all,
        dry_run=args.dry_run,
        stale_days=args.stale_days,
    )


if __name__ == "__main__":
    main()
