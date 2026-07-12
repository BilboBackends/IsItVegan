"""Repair historical vegan verdicts that name animal ingredients.

Dry-run by default. ``--apply`` appends an ``unclear`` classification rather
than rewriting history, records a guardrail audit row, and records the verdict
change used by the Admin menu-history view.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone

from config import settings
from guardrails import (
    defining_animal_ingredient,
    is_plant_based_venue,
    menu_declares_dish_vegan,
    unqualified_drink_animal_ingredient,
    unqualified_animal_ingredient,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


_LATEST_VEGAN_ROWS = """
SELECT d.id AS dish_id, d.restaurant_id, d.name, d.raw_description,
       d.price, d.category, c.verdict, c.confidence, c.reasoning, c.source_id,
       c.model_version, c.dairy_status, c.gluten_status, c.nut_status,
       c.protein_level, c.serving_role, c.meal_types, c.key_ingredients,
       c.alcohol_status, r.name AS restaurant_name, r.editorial_summary
FROM dishes d
JOIN restaurants r ON r.id = d.restaurant_id
JOIN classifications c ON c.id = (
    SELECT newest.id FROM classifications newest
    WHERE newest.dish_id = d.id
    ORDER BY newest.created_at DESC, newest.id DESC
    LIMIT 1
)
WHERE c.verdict IN ('vegan', 'likely_vegan')
ORDER BY r.name COLLATE NOCASE, d.name COLLATE NOCASE
"""


def _menu_text(conn: sqlite3.Connection, restaurant_id: int) -> str:
    rows = conn.execute(
        """
        SELECT content FROM sources
        WHERE restaurant_id = ? AND type = 'text'
          AND (url IS NULL OR url != 'google:editorial_summary')
        ORDER BY id
        """,
        (restaurant_id,),
    ).fetchall()
    return "\n\n".join(row["content"] or "" for row in rows)


def find_candidates(conn: sqlite3.Connection) -> tuple[list[dict], int]:
    conn.row_factory = sqlite3.Row
    candidates: list[dict] = []
    plant_venue_skips = 0
    venue_cache: dict[int, bool] = {}
    for raw in conn.execute(_LATEST_VEGAN_ROWS).fetchall():
        row = dict(raw)
        restaurant_id = int(row["restaurant_id"])
        if restaurant_id not in venue_cache:
            venue_cache[restaurant_id] = is_plant_based_venue(
                row["restaurant_name"],
                row.get("editorial_summary"),
                _menu_text(conn, restaurant_id),
            )
        menu_words = f"{row['name']} {row.get('raw_description') or ''}"
        if menu_declares_dish_vegan(row.get("raw_description")):
            continue
        if row.get("category") == "drink":
            words = row["name"]
            risky = unqualified_drink_animal_ingredient(words)
        else:
            words = menu_words
            offending = defining_animal_ingredient(row["name"], words)
            risky = offending or unqualified_animal_ingredient(words)
        if not risky:
            continue
        if venue_cache[restaurant_id]:
            plant_venue_skips += 1
            continue
        row["offending"] = risky
        candidates.append(row)
    return candidates, plant_venue_skips


def apply_candidates(conn: sqlite3.Connection, candidates: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        for row in candidates:
            old_verdict = row["verdict"]
            confidence = min(float(row["confidence"] or 0.0), 0.4)
            reason = (
                (row.get("reasoning") or "").rstrip()
                + f" [safety repair: {row['offending']} is unqualified in the menu text; "
                "vegan verdict downgraded for review]"
            ).strip()
            conn.execute(
                """
                INSERT INTO classifications
                    (dish_id, verdict, confidence, reasoning, source_id,
                     model_version, created_at, dairy_status, gluten_status,
                     nut_status, protein_level, serving_role, meal_types,
                     key_ingredients, alcohol_status)
                VALUES (?, 'unclear', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["dish_id"], confidence, reason, row["source_id"],
                    row["model_version"], now, row["dairy_status"],
                    row["gluten_status"], row["nut_status"],
                    row["protein_level"], row["serving_role"],
                    row["meal_types"], row["key_ingredients"],
                    row["alcohol_status"],
                ),
            )
            provider = (
                "deepseek"
                if str(row.get("model_version") or "").lower().startswith("deepseek")
                else "historical"
            )
            conn.execute(
                """
                INSERT INTO classification_audits
                    (restaurant_id, dish_name, provider, model, check_type,
                     rule, status, detail, expected_verdict, actual_verdict,
                     created_at)
                VALUES (?, ?, ?, ?, 'guardrail',
                        'animal_ingredient_vegan_backfill', 'downgraded',
                        ?, 'unclear', ?, ?)
                """,
                (
                    row["restaurant_id"], row["name"], provider,
                    row["model_version"],
                    f"{row['offending']} found in stored menu words",
                    old_verdict, now,
                ),
            )
            conn.execute(
                """
                INSERT INTO dish_changes
                    (restaurant_id, observed_at, change_type, dish_name,
                     old_price, new_price, old_verdict, new_verdict)
                VALUES (?, ?, 'verdict_changed', ?, ?, ?, ?, 'unclear')
                """,
                (
                    row["restaurant_id"], now, row["name"], row["price"],
                    row["price"], old_verdict,
                ),
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Downgrade unsafe historical vegan verdicts to unclear."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database", default=settings.database_path)
    args = parser.parse_args()

    conn = sqlite3.connect(args.database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        candidates, plant_skips = find_candidates(conn)
        for row in candidates:
            print(
                f"{row['dish_id']:>6}  {row['verdict']:<13}  "
                f"{row['restaurant_name']} — {row['name']} "
                f"[{row['offending']}]"
            )
        models = Counter(row.get("model_version") or "unknown" for row in candidates)
        action = "Applied" if args.apply else "Would repair"
        if args.apply and candidates:
            apply_candidates(conn, candidates)
        print(
            f"\n{action} {len(candidates)} classification(s); "
            f"skipped {plant_skips} risky-name row(s) at explicitly "
            "plant-based venues."
        )
        if models:
            print("Models: " + ", ".join(f"{name}={count}" for name, count in models.items()))
        if not args.apply:
            print("Dry run only. Re-run with --apply after reviewing this list.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
