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
from venue_filter import is_consumer_food_venue

_VEGANISH = ("vegan", "likely_vegan", "vegan_adaptable")


def _targets(
    restaurant_id: int | None,
    do_all: bool,
    restaurant_ids: list[int] | None = None,
) -> list[dict]:
    all_restaurants = {r["id"]: r for r in db.list_restaurants()}
    if restaurant_id is not None:
        r = all_restaurants.get(restaurant_id)
        if r is None:
            raise SystemExit(f"No restaurant with id {restaurant_id}.")
        if not r.get("has_menu_text"):
            raise SystemExit(f"Restaurant {restaurant_id} has no menu text.")
        return [r]
    eligible = [
        r
        for r in all_restaurants.values()
        if r.get("has_menu_text")
        and r.get("refresh_enabled", 1)
        and is_consumer_food_venue(r)
    ]
    if restaurant_ids is not None:
        requested = set(restaurant_ids)
        return [r for r in eligible if r["id"] in requested]
    if do_all:
        return eligible
    needing = set(db.restaurants_needing_classification())
    return [r for r in eligible if r["id"] in needing]


def run(
    restaurant_id: int | None = None,
    do_all: bool = False,
    dry_run: bool = False,
    mock: bool = False,
    restaurant_ids: list[int] | None = None,
    on_progress=None,
    should_stop=None,
    provider: str | None = None,
) -> dict:
    """Classify targets; on_progress (optional) receives event dicts so a live
    caller (the Admin dashboard) can show progress and per-restaurant cost:
    {"total": N}, {"current": name}, {"result": {..., "cost": $}}.
    should_stop (optional) is checked between restaurants so a background job
    can stop without interrupting an API response or a database write.
    """
    def _emit(event: dict) -> None:
        if on_progress is not None:
            on_progress(event)

    db.init_db()
    targets = _targets(restaurant_id, do_all, restaurant_ids)
    _emit({"total": len(targets)})
    print(f"Classifying {len(targets)} restaurant(s)...\n")

    ok_count = fail_count = dish_count = 0
    total_cost = 0.0
    failures: list[tuple[str, str]] = []
    cancelled = False
    used_provider = provider
    used_billing = None

    for r in targets:
        if should_stop is not None and should_stop():
            cancelled = True
            break
        _emit({"current": r["name"]})
        source = db.get_menu_text(r["id"])
        if source is None:
            _emit({"result": {"name": r["name"], "ok": False,
                              "error": "no menu text"}})
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
            provider=provider,
        )
        used_provider = result.provider
        used_billing = result.billing
        if not result.ok:
            fail_count += 1
            failures.append((r["name"], result.error or "unknown"))
            print(f"  [fail] {r['name']} — {result.error}")
            _emit({"result": {"name": r["name"], "ok": False,
                              "error": result.error}})
            continue

        veganish = sum(
            1
            for d in result.dishes
            if d.verdict in _VEGANISH and d.category != "drink"
        )
        ok_count += 1
        dish_count += len(result.dishes)
        total_cost += result.cost_estimate
        print(
            f"  [ok]   {r['name']}: {len(result.dishes)} dishes, "
            f"{veganish} vegan/likely/adaptable (food, excl. drinks)"
            + (
                f"  [~${result.cost_estimate:.2f}]"
                if result.billing == "api"
                else f"  [{result.provider} subscription]"
            )
        )
        _emit({"result": {
            "name": r["name"], "ok": True, "dishes": len(result.dishes),
            "veganish": veganish,
            "cost": (
                round(result.cost_estimate, 3)
                if result.billing == "api"
                else None
            ),
            "provider": result.provider,
            "billing": result.billing,
        }})

        if dry_run:
            continue
        classified_at = datetime.now(timezone.utc).isoformat()
        db.record_classify_cost(
            r["id"],
            result.cost_estimate if result.billing == "api" else None,
            provider=result.provider,
        )
        # Fresh snapshot: drop old dishes so items that left the menu don't
        # linger with stale verdicts.
        db.delete_dishes_for_restaurant(r["id"])
        for d in result.dishes:
            dish_id = db.upsert_dish(
                r["id"], d.name, d.description, d.price, category=d.category
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
                created_at=classified_at,
                dairy_status=d.dairy_status,
                gluten_status=d.gluten_status,
                nut_status=d.nut_status,
                protein_level=d.protein_level,
                serving_role=d.serving_role,
                meal_types=d.meal_types,
                key_ingredients=d.key_ingredients,
            )

    status = "Stopped" if cancelled else "Done"
    print(
        f"\n{status}. {ok_count} restaurants classified ({dish_count} dishes), "
        f"{fail_count} failed. Estimated API cost: ~${total_cost:.2f}."
    )
    if failures:
        print("Failures:")
        for name, err in failures:
            print(f"  - {name}: {err}")
    if dry_run:
        print("[dry-run] Nothing written to the database.")
    return {"ok": ok_count, "failed": fail_count, "dishes": dish_count,
            "cost": round(total_cost, 2), "failures": failures,
            "cancelled": cancelled, "provider": used_provider,
            "billing": used_billing}


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 3 dish classification.")
    parser.add_argument("--restaurant-id", type=int, default=None)
    parser.add_argument("--all", action="store_true",
                        help="Re-classify all restaurants with menu text.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mock", action="store_true",
                        help="Use a canned result instead of calling the API.")
    parser.add_argument(
        "--provider", default=None,
        help="auto | claude | codex | anthropic, or a comma-separated "
        "priority list (e.g. claude,codex). auto = claude, codex, anthropic.",
    )
    args = parser.parse_args()
    run(restaurant_id=args.restaurant_id, do_all=args.all,
        dry_run=args.dry_run, mock=args.mock, provider=args.provider)


if __name__ == "__main__":
    main()
