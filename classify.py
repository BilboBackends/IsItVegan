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
import hashlib
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


def _menu_hash(text: str) -> str:
    """Whitespace-insensitive fingerprint of the menu text a classification
    ran on — recrawls that reflow the same content shouldn't trigger work."""
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# Delta results claiming more than this fraction of prior dishes vanished are
# distrusted — that's a misread menu, not a menu change. Falls back to full.
_MAX_DELTA_REMOVED_FRACTION = 0.6


def run(
    restaurant_id: int | None = None,
    do_all: bool = False,
    dry_run: bool = False,
    mock: bool = False,
    restaurant_ids: list[int] | None = None,
    on_progress=None,
    should_stop=None,
    provider: str | None = None,
    parallel: int = 3,
    mode: str = "auto",
) -> dict:
    """Classify targets; on_progress (optional) receives event dicts so a live
    caller (the Admin dashboard) can show progress and per-restaurant cost:
    {"total": N}, {"current": name}, {"result": {..., "cost": $}}.
    should_stop (optional) is checked before each new restaurant starts so a
    background job can stop without interrupting a model call in flight.

    parallel: how many restaurants classify CONCURRENTLY (capped at 6). Model
    calls are I/O-bound (API/CLI round-trips), so a small worker pool cuts a
    bulk run's wall time near-linearly. Only the model call runs in worker
    threads — every SQLite write and progress event happens on this thread,
    so persistence stays serial and safe.

    mode:
      auto (default) — skip restaurants whose menu text is UNCHANGED since
        their last classification; use DELTA classification (only new/changed
        dishes are emitted, removed ones listed) when a prior dish inventory
        exists; full otherwise. Suspicious deltas fall back to full.
      full — always re-extract the whole menu (schema upgrades, distrusted
        prior data).
    """
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    def _emit(event: dict) -> None:
        if on_progress is not None:
            on_progress(event)

    db.init_db()
    targets = _targets(restaurant_id, do_all, restaurant_ids)
    _emit({"total": len(targets)})
    workers = max(1, min(int(parallel or 1), 6))
    print(
        f"Classifying {len(targets)} restaurant(s)"
        + (f" ({workers} in parallel)" if workers > 1 else "")
        + (f" [mode={mode}]" if mode != "auto" else "")
        + "...\n"
    )

    ok_count = fail_count = dish_count = skipped_count = 0
    total_cost = 0.0
    failures: list[tuple[str, str]] = []
    cancelled = False
    used_provider = provider
    used_billing = None

    def _classify_target(r: dict):
        """Worker-thread part: read menu text, call the model. No writes.

        Returns (source, result, prior_snapshot, text_hash). result is None
        with prior=None for "no menu text"; the string "skipped" stands in
        for a result when the menu is unchanged since last classification.
        """
        source = db.get_menu_text(r["id"])
        if source is None:
            return None, None, None, None
        text_hash = _menu_hash(source["content"])
        prior = db.snapshot_dishes(r["id"])

        if (
            mode == "auto"
            and not mock
            and prior
            and r.get("last_classified_hash")
            and r["last_classified_hash"] == text_hash
        ):
            return source, "skipped", prior, text_hash

        use_delta = mode == "auto" and not mock and len(prior) >= 5
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
            prior_dishes=prior if use_delta else None,
        )
        # Distrust a delta that erases most of the menu — more likely a
        # misread than a real change. Re-run as a full extraction.
        if (
            use_delta
            and result.ok
            and len(result.removed_dish_names)
            > max(3, _MAX_DELTA_REMOVED_FRACTION * len(prior))
        ):
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
        return source, result, prior, text_hash

    def _persist_dish(r: dict, source, result, d, classified_at: str) -> None:
        dish_id = db.upsert_dish(
            r["id"],
            d.name,
            d.description,
            d.price,
            category=d.category,
            calories=d.calories,
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

    def _handle(r: dict, source, result, prior, text_hash) -> None:
        """Coordinator-thread part: counters, events, and ALL DB writes."""
        nonlocal ok_count, fail_count, dish_count, skipped_count, total_cost
        nonlocal used_provider, used_billing
        if source is None:
            _emit({"result": {"name": r["name"], "ok": False,
                              "error": "no menu text"}})
            return
        if result == "skipped":
            skipped_count += 1
            print(f"  [skip] {r['name']} — menu unchanged since last classification")
            _emit({"result": {"name": r["name"], "ok": True, "skipped": True,
                              "dishes": len(prior or {}), "cost": None}})
            return
        used_provider = result.provider
        used_billing = result.billing
        if not result.ok:
            fail_count += 1
            failures.append((r["name"], result.error or "unknown"))
            print(f"  [fail] {r['name']} — {result.error}")
            _emit({"result": {"name": r["name"], "ok": False,
                              "error": result.error}})
            return

        delta = result.mode == "delta"
        veganish = sum(
            1
            for d in result.dishes
            if d.verdict in _VEGANISH and d.category != "drink"
        )
        ok_count += 1
        dish_count += len(result.dishes)
        total_cost += result.cost_estimate
        print(
            f"  [ok]   {r['name']}: "
            + (
                f"delta — {len(result.dishes)} new/changed, "
                f"{len(result.removed_dish_names)} removed"
                if delta
                else f"{len(result.dishes)} dishes, "
                f"{veganish} vegan/likely/adaptable (food, excl. drinks)"
            )
            + (
                f"  [~${result.cost_estimate:.2f}]"
                if result.billing == "api"
                else f"  [{result.provider} subscription]"
            )
        )
        _emit({"result": {
            "name": r["name"], "ok": True, "dishes": len(result.dishes),
            "veganish": veganish,
            "mode": result.mode,
            "removed": len(result.removed_dish_names) if delta else None,
            "cost": (
                round(result.cost_estimate, 3)
                if result.billing == "api"
                else None
            ),
            "provider": result.provider,
            "billing": result.billing,
        }})

        if dry_run:
            return
        classified_at = datetime.now(timezone.utc).isoformat()
        db.record_classify_cost(
            r["id"],
            result.cost_estimate if result.billing == "api" else None,
            provider=result.provider,
        )

        if delta:
            # Surgical update: unchanged dishes keep their rows and verdicts.
            for d in result.dishes:
                _persist_dish(r, source, result, d, classified_at)
            for name in result.removed_dish_names:
                db.delete_dish(r["id"], name)
        else:
            # Fresh snapshot: drop old dishes so items that left the menu
            # don't linger with stale verdicts.
            db.delete_dishes_for_restaurant(r["id"])
            for d in result.dishes:
                _persist_dish(r, source, result, d, classified_at)

        merged_duplicates = db.deduplicate_dishes_for_restaurant(r["id"])
        if merged_duplicates:
            print(
                f"         merged {len(merged_duplicates)} duplicate dish "
                f"group(s) differing only by formatting"
            )

        # Longitudinal record: how this menu drifted since last time. First
        # classifications are skipped — 150 "added" rows say nothing.
        if prior:
            changes = db.compute_dish_changes(prior, db.snapshot_dishes(r["id"]))
            if changes:
                db.record_dish_changes(r["id"], changes, observed_at=classified_at)
        if text_hash:
            db.set_last_classified_hash(r["id"], text_hash)

        # Monitoring trail for the cheap tier: every guardrail flag or
        # downgrade is persisted so Admin can watch the flag rate.
        if getattr(result, "guardrail_flags", None):
            db.record_audits(
                result.guardrail_flags,
                provider=result.provider,
                model=result.model,
                restaurant_id=r["id"],
            )
            print(
                f"         guardrails: {len(result.guardrail_flags)} flag(s) "
                f"recorded for audit"
            )

    queue = list(targets)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending: dict = {}

        def _submit_more() -> None:
            nonlocal cancelled
            while queue and len(pending) < workers:
                if should_stop is not None and should_stop():
                    cancelled = True
                    queue.clear()
                    return
                target = queue.pop(0)
                _emit({"current": target["name"]})
                pending[pool.submit(_classify_target, target)] = target

        _submit_more()
        while pending:
            done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
            for future in done:
                target = pending.pop(future)
                try:
                    source, result, prior, text_hash = future.result()
                except Exception as exc:
                    fail_count += 1
                    failures.append((target["name"], f"{type(exc).__name__}: {exc}"))
                    print(f"  [fail] {target['name']} — {exc}")
                    _emit({"result": {"name": target["name"], "ok": False,
                                      "error": str(exc)}})
                    continue
                _handle(target, source, result, prior, text_hash)
            _submit_more()

    status = "Stopped" if cancelled else "Done"
    print(
        f"\n{status}. {ok_count} restaurants classified ({dish_count} dishes), "
        f"{skipped_count} skipped (unchanged), {fail_count} failed. "
        f"Estimated API cost: ~${total_cost:.2f}."
    )
    if failures:
        print("Failures:")
        for name, err in failures:
            print(f"  - {name}: {err}")
    if dry_run:
        print("[dry-run] Nothing written to the database.")
    return {"ok": ok_count, "failed": fail_count, "dishes": dish_count,
            "skipped": skipped_count,
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
    parser.add_argument(
        "--parallel", type=int, default=3,
        help="How many restaurants to classify concurrently (1-6, default 3).",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Force full re-extraction (default: skip unchanged menus and "
        "classify only the changes when a prior inventory exists).",
    )
    args = parser.parse_args()
    run(restaurant_id=args.restaurant_id, do_all=args.all,
        dry_run=args.dry_run, mock=args.mock, provider=args.provider,
        parallel=args.parallel, mode="full" if args.full else "auto")


if __name__ == "__main__":
    main()
