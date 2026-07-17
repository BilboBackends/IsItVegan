"""Second-pass dish attribute enrichment (vegetarian, allergens, character).

Backfills discovery attributes onto existing classifications without
re-scraping or re-extracting menus: each dish's name, description, and
existing verdict are sent to DeepSeek in small batches, and the structured
answers land on the dish's latest classification row.

Independently runnable stage (like discovery/ingest/classify):

    python dish_attributes.py --limit 100        # sanity slice
    python dish_attributes.py                    # full resumable backfill
    python dish_attributes.py --restaurant 42    # one restaurant

Drinks are skipped entirely — spice level and cooking method are noise for
a prosecco. Resume is automatic: rows with attributes_enriched_at set are
never re-sent, so an interrupted run continues where it stopped.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import db
from classification_providers import ProviderResponse, run_provider

BATCH_SIZE = 25

ENUM_FIELDS: dict[str, list[str]] = {
    "vegetarian_status": ["vegetarian", "not_vegetarian", "unclear"],
    "protein_source": [
        "meat_analogue", "tofu_tempeh_seitan", "legume", "nut",
        "animal", "none", "unclear",
    ],
    "egg_status": ["contains", "free", "unclear"],
    "soy_status": ["contains", "free", "unclear"],
    "sesame_status": ["contains", "free", "unclear"],
    "spice_level": ["none", "mild", "medium", "hot", "unclear"],
    # roasted/stir_fry joined after the first full pass: the model kept
    # (correctly) insisting on them for rotisserie and wok dishes.
    "cooking_method": [
        "fried", "grilled", "baked", "roasted", "raw", "steamed",
        "boiled_simmered", "sauteed", "stir_fry", "mixed", "unclear",
    ],
    # What the dish is like to eat. Recognizable named types stay coarse
    # enough to filter on; "other" is the catch-all (the DB's 'unclear'
    # default means "not enriched yet", so the model never says it).
    "dish_format": [
        "bowl", "poke", "sushi", "ramen", "pho", "noodle_dish", "pasta",
        "pizza", "flatbread", "sandwich", "wrap", "burrito", "taco",
        "quesadilla", "nachos", "burger", "hot_dog", "gyro", "kebab",
        "dumpling", "empanada", "spring_roll", "curry", "stir_fry",
        "fried_rice", "rice_dish", "soup", "stew", "chili", "salad",
        "small_plate", "plate", "breakfast_plate", "omelet",
        "pancake_waffle", "baked_good", "pastry", "dessert", "drink",
        "other",
    ],
}

FLAVOR_TAGS = [
    "creamy_rich", "fresh_light", "tangy", "sweet", "smoky", "savory",
    "herbal", "garlicky", "umami",
]

MEAT_SOURCES = [
    "beef", "pork", "chicken", "turkey", "duck", "lamb", "goat", "veal",
    "fish", "shellfish", "other_meat",
]

SYSTEM_PROMPT = f"""You enrich restaurant menu dishes with structured dietary and character attributes.

You receive dishes that were already classified for vegan status. For each dish, using ONLY the dish name, description, category, restaurant name, and the existing classification as evidence, produce:

- vegetarian_status: "vegetarian" only if no meat, poultry, fish, seafood, gelatin, or meat-based stock/sauce is present or likely. Dairy and eggs are fine for vegetarian. "not_vegetarian" if any is present or strongly implied (e.g., fish sauce in a Thai curry unless stated otherwise). Every vegan or likely-vegan dish is vegetarian.
- protein_source: the dish's PRIMARY protein. "meat_analogue" only for explicit substitutes (Impossible, Beyond, plant-based chick'n/sausage, vegan meat). "tofu_tempeh_seitan" for those three. "legume" (beans, lentils, chickpeas, peas, peanuts), "nut", "animal" (any meat/fish/dairy/egg protein), "none" (no notable protein, e.g. a side salad or fries), or "unclear".
- egg_status / soy_status / sesame_status: "contains" if the ingredient or a derivative (mayo/aioli/most waffles and pancakes=egg; tofu, soy sauce, miso, edamame=soy; tahini, sesame oil/seeds=sesame) is present or standard in the dish as described; "free" ONLY when the ingredients are enumerated well enough to rule it out; "unclear" when preparation is unknown. A wrong "free" on an allergen is harmful; "unclear" never is.
- meat_sources: 0-4 values from {json.dumps(MEAT_SOURCES)} listing every meat/seafood present or definitionally standard in the dish (pepperoni -> pork; carbonara -> pork; caesar dressing -> fish; surf and turf -> beef and shellfish). "other_meat" for meats not listed (rabbit, alligator) or unnamed generic meat. Empty array when no meat is identified — including every vegan/vegetarian dish.
- spice_level: only from explicit evidence (chili markers, "spicy", "hot", named hot sauces/peppers, dishes definitionally hot like vindaloo). "none" for clearly mild dishes; otherwise "unclear".
- cooking_method: the dominant stated or definitionally certain method ("fried" for tempura/crispy; "baked" for pizza). "raw" only when raw preparation is the point (salads, poke, crudo, ceviche) — a cold assembled sandwich is "unclear". "mixed" if two methods are equally central.
- dish_format: what the dish is like to eat, from the allowed list only. Prefer the most specific value ("pho" over "soup", "burrito" over "wrap", "sushi" over "small_plate"). "plate" is a standard entree plate; "small_plate" covers appetizers, tapas, and sides. Use "other" when nothing fits.
- flavor_profile: 0-3 tags from {json.dumps(FLAVOR_TAGS)} that the DESCRIPTION supports (creamy coconut broth -> creamy_rich; citrus/fresh herbs -> fresh_light). Empty array when the text gives no flavor evidence.
- ingredient_tags: up to 6 lowercase singular canonical ingredient nouns actually named or definitionally certain (e.g. "chickpea" not "crispy chickpeas"; "mushroom" not "wild mushroom blend"). No cooking methods, no adjectives.
- adaptation: ONLY when the existing verdict is vegan_adaptable: one short imperative sentence for the diner ("Ask for no cheese."). Otherwise null.

THE MOST IMPORTANT RULE: "unclear" (or an empty array / null) is the CORRECT answer whenever the text does not give evidence. Do not guess plausible values.

Return every dish id you were given, exactly once."""

RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["dishes"],
    "properties": {
        "dishes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id"] + list(ENUM_FIELDS) + [
                    "meat_sources", "flavor_profile", "ingredient_tags",
                    "adaptation",
                ],
                "properties": {
                    "id": {"type": "integer"},
                    **{
                        field: {"enum": allowed}
                        for field, allowed in ENUM_FIELDS.items()
                    },
                    "meat_sources": {
                        "type": "array",
                        "items": {"enum": MEAT_SOURCES},
                        "maxItems": 4,
                    },
                    "flavor_profile": {
                        "type": "array",
                        "items": {"enum": FLAVOR_TAGS},
                        "maxItems": 3,
                    },
                    "ingredient_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 6,
                    },
                    "adaptation": {"type": ["string", "null"]},
                },
            },
        }
    },
}


def build_user_prompt(batch: list[dict]) -> str:
    payload = [
        {
            "id": row["dish_id"],
            "name": row["name"],
            "description": row["raw_description"] or "",
            "category": row["category"] or "food",
            "restaurant": row["restaurant"],
            "existing": {
                "verdict": row["verdict"],
                "dairy_status": row["dairy_status"],
                "gluten_status": row["gluten_status"],
                "nut_status": row["nut_status"],
            },
        }
        for row in batch
    ]
    return "Dishes to enrich:\n" + json.dumps(payload, ensure_ascii=False)


def validate_rows(
    batch: list[dict], data: dict | None
) -> tuple[dict[int, dict], list[str]]:
    """Structural validation of one model response.

    Returns (attributes per dish id, problem strings). Invalid rows are
    dropped individually so one bad enum doesn't discard the batch.
    """
    problems: list[str] = []
    accepted: dict[int, dict] = {}
    rows = (data or {}).get("dishes")
    if not isinstance(rows, list):
        return {}, ["response has no dishes array"]
    expected = {row["dish_id"] for row in batch}
    for row in rows:
        if not isinstance(row, dict) or row.get("id") not in expected:
            got = row.get("id") if isinstance(row, dict) else repr(row)
            problems.append(f"unexpected id: {got}")
            continue
        issues = [
            f"{field}={row.get(field)!r}"
            for field, allowed in ENUM_FIELDS.items()
            if row.get(field) not in allowed
        ]
        meats = row.get("meat_sources")
        if not isinstance(meats, list) or any(
            meat not in MEAT_SOURCES for meat in meats
        ):
            issues.append(f"meat_sources={meats!r}")
        flavors = row.get("flavor_profile")
        if not isinstance(flavors, list) or any(
            tag not in FLAVOR_TAGS for tag in flavors
        ):
            issues.append(f"flavor_profile={flavors!r}")
        tags = row.get("ingredient_tags")
        if not isinstance(tags, list) or any(
            not isinstance(tag, str) or not tag.strip() for tag in tags
        ):
            issues.append(f"ingredient_tags={tags!r}")
        adaptation = row.get("adaptation")
        if adaptation is not None and not isinstance(adaptation, str):
            issues.append(f"adaptation={adaptation!r}")
        if issues:
            problems.append(f"dish {row['id']}: " + ", ".join(issues))
            continue
        accepted[row["id"]] = {
            **{field: row[field] for field in ENUM_FIELDS},
            "meat_sources": list(dict.fromkeys(meats))[:4],
            "flavor_profile": flavors[:3],
            "ingredient_tags": [tag.strip().lower() for tag in tags][:6],
            "adaptation": (adaptation or "").strip() or None,
        }
    for dish_id in sorted(expected - set(accepted)):
        if not any(f"dish {dish_id}:" in p for p in problems):
            problems.append(f"dish {dish_id}: missing from response")
    return accepted, problems


def pending_work(
    db_path: str | None = None,
    restaurant_id: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Latest classification per non-drink dish, not yet enriched."""
    query = """
        SELECT c.id AS classification_id, d.id AS dish_id, d.name,
               d.raw_description, d.category, r.name AS restaurant,
               c.verdict, c.dairy_status, c.gluten_status, c.nut_status
        FROM dishes d
        JOIN restaurants r ON r.id = d.restaurant_id AND r.archived = 0
        JOIN classifications c ON c.dish_id = d.id
        WHERE c.id = (SELECT MAX(c2.id) FROM classifications c2
                      WHERE c2.dish_id = d.id)
          AND c.attributes_enriched_at IS NULL
          AND COALESCE(d.category, 'food') != 'drink'
    """
    params: list = []
    if restaurant_id is not None:
        query += " AND d.restaurant_id = ?"
        params.append(restaurant_id)
    query += " ORDER BY d.restaurant_id, d.id"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    with db.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(query, params)]


def enrich_batch(batch: list[dict]) -> tuple[ProviderResponse, dict[int, dict], list[str]]:
    """One DeepSeek call (retry once on malformed/invalid output)."""
    response = run_provider(
        requested="deepseek",
        system_prompt=SYSTEM_PROMPT,
        user_prompt=build_user_prompt(batch),
        schema=RESPONSE_SCHEMA,
    )
    accepted, problems = validate_rows(batch, response.data if response.ok else None)
    if len(accepted) < len(batch):
        retry = run_provider(
            requested="deepseek",
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(batch),
            schema=RESPONSE_SCHEMA,
        )
        retry_accepted, retry_problems = validate_rows(
            batch, retry.data if retry.ok else None
        )
        if len(retry_accepted) > len(accepted):
            merged = ProviderResponse(
                ok=retry.ok, provider=retry.provider, model=retry.model,
                billing=retry.billing, data=retry.data, error=retry.error,
                input_tokens=response.input_tokens + retry.input_tokens,
                output_tokens=response.output_tokens + retry.output_tokens,
                cost_estimate=response.cost_estimate + retry.cost_estimate,
            )
            return merged, retry_accepted, retry_problems
        response.input_tokens += retry.input_tokens
        response.output_tokens += retry.output_tokens
        response.cost_estimate += retry.cost_estimate
    return response, accepted, problems


def store_attributes(
    batch: list[dict],
    accepted: dict[int, dict],
    model: str,
    db_path: str | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    by_dish = {row["dish_id"]: row["classification_id"] for row in batch}
    with db.connect(db_path) as conn:
        for dish_id, attrs in accepted.items():
            conn.execute(
                """
                UPDATE classifications SET
                  vegetarian_status = ?, protein_source = ?, egg_status = ?,
                  soy_status = ?, sesame_status = ?, spice_level = ?,
                  cooking_method = ?, dish_format = ?, meat_sources = ?,
                  flavor_profile = ?,
                  ingredient_tags = ?, vegan_adaptation = ?,
                  attributes_enriched_at = ?, attributes_model = ?
                WHERE id = ?
                """,
                (
                    attrs["vegetarian_status"], attrs["protein_source"],
                    attrs["egg_status"], attrs["soy_status"],
                    attrs["sesame_status"], attrs["spice_level"],
                    attrs["cooking_method"], attrs["dish_format"],
                    json.dumps(attrs["meat_sources"]),
                    json.dumps(attrs["flavor_profile"]),
                    json.dumps(attrs["ingredient_tags"]),
                    attrs["adaptation"], now, model,
                    by_dish[dish_id],
                ),
            )
    return len(accepted)


def run_backfill(
    db_path: str | None = None,
    restaurant_id: int | None = None,
    limit: int | None = None,
    workers: int = 6,
    dry_run: bool = False,
) -> dict:
    work = pending_work(db_path, restaurant_id=restaurant_id, limit=limit)
    batches = [
        work[start:start + BATCH_SIZE]
        for start in range(0, len(work), BATCH_SIZE)
    ]
    print(f"{len(work)} dishes pending in {len(batches)} batches "
          f"(drinks excluded; already-enriched rows skipped)", flush=True)
    if dry_run or not work:
        return {"pending": len(work), "stored": 0, "cost": 0.0}

    stored = failed = 0
    cost = 0.0
    started = time.monotonic()
    problems_log: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(enrich_batch, b): b for b in batches}
        for index, future in enumerate(as_completed(futures), 1):
            batch = futures[future]
            try:
                response, accepted, problems = future.result()
            except Exception as exc:  # keep the run alive; rows stay pending
                failed += len(batch)
                problems_log.append(f"batch crashed: {exc}")
                continue
            cost += response.cost_estimate
            problems_log.extend(problems)
            if accepted:
                stored += store_attributes(
                    batch, accepted, response.model, db_path
                )
            failed += len(batch) - len(accepted)
            if index % 20 == 0 or index == len(batches):
                elapsed = time.monotonic() - started
                rate = stored / elapsed if elapsed else 0.0
                remaining = len(work) - stored - failed
                eta_min = (remaining / rate / 60) if rate else 0.0
                print(
                    f"[{index}/{len(batches)}] stored {stored}, "
                    f"failed {failed}, ${cost:.2f}, "
                    f"{rate:.1f} dishes/s, ~{eta_min:.0f} min left",
                    flush=True,
                )

    if problems_log:
        print(f"{len(problems_log)} problems (rows left pending for a rerun):")
        for problem in problems_log[:40]:
            print(" -", problem)
        if len(problems_log) > 40:
            print(f"   … and {len(problems_log) - 40} more")
    print(f"Done: {stored} enriched, {failed} left pending, ${cost:.2f}")
    return {"pending": len(work), "stored": stored, "cost": cost}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restaurant", type=int, default=None,
                        help="only this restaurant id")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the number of dishes this run")
    parser.add_argument("--workers", type=int, default=6,
                        help="parallel DeepSeek calls (default 6)")
    parser.add_argument("--dry-run", action="store_true",
                        help="count pending work without calling the model")
    args = parser.parse_args()

    db.init_db()
    run_backfill(
        restaurant_id=args.restaurant,
        limit=args.limit,
        workers=args.workers,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
