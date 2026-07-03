"""Phase 3 CLI: classify dishes for restaurants with scraped menu text.

For each restaurant with a real menu-text source, sends the menu (plus Google
context) to Claude, upserts the extracted dishes, and stores a classification
per dish (verdict, confidence, reasoning, source link, model version).

Runnable in isolation (per CLAUDE.md conventions):

    python classify.py                     # classify restaurants not yet done
    python classify.py --all               # re-classify everyone with a menu
    python classify.py --restaurant-id 14  # just one (debugging)
    python classify.py --mock              # no API call, canned result
    python classify.py --dry-run           # classify but don't write to DB
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db
from classifier import classify_menu

_VEGANISH = ("vegan", "likely_vegan", "vegan_adaptable")


def _targets(restaurant_id: int | None, do_all: bool) -> list[dict]:
    all_restaurants = {r["id"]: r for r in db.list_restaurants()}
    if restaurant_id is not None:
        r = all_restaurants.get(restaurant_id)
        if r is None:
            raise SystemExit(f"No restaurant with id {restaurant_id}.")
        if not r.get("has_menu_text"):
            raise SystemExit(f"Restaurant {restaurant_id} has no menu text.")
        return [r]
    if do_all:
        return [r for r in all_restaurants.values() if r.get("has_menu_text")]
    needing = set(db.restaurants_needing_classification())
    return [r for r in all_restaurants.values() if r["id"] in needing]


def run(
    restaurant_id: int | None = None,
    do_all: bool = False,
    dry_run: bool = False,
    mock: bool = False,
) -> dict:
    db.init_db()
    targets = _targets(restaurant_id, do_all)
    print(f"Classifying {len(targets)} restaurant(s)...\n")

    now = datetime.now(timezone.utc).isoformat()
    ok_count = fail_count = dish_count = 0
    failures: list[tuple[str, str]] = []

    for r in targets:
        source = db.get_menu_text(r["id"])
        if source is None:
            continue
        result = classify_menu(
            source["content"],
            restaurant_name=r["name"],
            editorial_summary=r.get("editorial_summary"),
            serves_vegetarian=(
                None
                if r.get("serves_vegetarian") is None
                else bool(r["serves_vegetarian"])
            ),
            mock=mock,
        )
        if not result.ok:
            fail_count += 1
            failures.append((r["name"], result.error or "unknown"))
            print(f"  [fail] {r['name']} — {result.error}")
            continue

        veganish = sum(1 for d in result.dishes if d.verdict in _VEGANISH)
        ok_count += 1
        dish_count += len(result.dishes)
        print(
            f"  [ok]   {r['name']}: {len(result.dishes)} dishes, "
            f"{veganish} vegan/likely/adaptable"
        )

        if dry_run:
            continue
        for d in result.dishes:
            dish_id = db.upsert_dish(
                r["id"], d.name, d.description, d.price
            )
            # Evidence lives in reasoning text; source_id links the verdict to
            # the scraped menu source it came from (explainability, CLAUDE.md).
            reasoning = d.reasoning
            if d.evidence:
                reasoning = f"{d.reasoning} | evidence: “{d.evidence}”"
            db.insert_classification(
                dish_id=dish_id,
                verdict=d.verdict,
                confidence=d.confidence,
                reasoning=reasoning,
                source_id=source["id"],
                model_version=result.model,
                created_at=now,
            )

    print(
        f"\nDone. {ok_count} restaurants classified ({dish_count} dishes), "
        f"{fail_count} failed."
    )
    if failures:
        print("Failures:")
        for name, err in failures:
            print(f"  - {name}: {err}")
    if dry_run:
        print("[dry-run] Nothing written to the database.")
    return {"ok": ok_count, "failed": fail_count, "dishes": dish_count,
            "failures": failures}


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 3 dish classification.")
    parser.add_argument("--restaurant-id", type=int, default=None)
    parser.add_argument("--all", action="store_true",
                        help="Re-classify all restaurants with menu text.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mock", action="store_true",
                        help="Use a canned result instead of calling the API.")
    args = parser.parse_args()
    run(restaurant_id=args.restaurant_id, do_all=args.all,
        dry_run=args.dry_run, mock=args.mock)


if __name__ == "__main__":
    main()
