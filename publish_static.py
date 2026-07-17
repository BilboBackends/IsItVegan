"""Export consumer data as static JSON for the public site (GitHub Pages).

The public site has no backend: it serves the built frontend plus the JSON
snapshots this script writes to frontend/public/data/. Publishing flow:

    python publish_static.py            # export from the local SQLite DB
    python publish_static.py --push     # export + commit + push (deploys)

Only consumer-facing data ships: archived/hidden/non-food venues are
excluded (same venue_filter gate as the live consumer API), and admin-only
fields (costs, hashes, crawl bookkeeping) are stripped. No keys, no menus'
raw source text, no pipeline state.
"""
from __future__ import annotations

import argparse
import gzip
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db
from vegan_score import compute_vegan_score, menu_offers_plant_protein
from venue_filter import is_consumer_food_venue, is_consumer_ready

DATA_DIR = Path(__file__).parent / "frontend" / "public" / "data"
RESTAURANT_DISH_DIR = DATA_DIR / "restaurant-dishes"

# Keep the original full catalog at its historical URL for browser tabs that
# still have the pre-compact JavaScript bundle cached. New bundles load the
# compact v2 asset instead. The ignored plain file remains the local mutable
# copy used by targeted exports; Cloudflare omits it from deployments.
LEGACY_DISH_GZIP_NAME = "dishes.json.gz"
COMPACT_DISH_GZIP_NAME = "dishes-v2.json.gz"

# The consumer UI reads exactly these restaurant fields — admin bookkeeping
# (costs, hashes, refresh/crawl state) stays local.
_RESTAURANT_FIELDS = (
    # place_id is public Google data and the frontend's marker/focus key —
    # without it every card matched the "focused" check (undefined ===
    # undefined) and map pins collided on one key.
    "id", "name", "address", "place_id", "website_url", "lat", "lng",
    "primary_type", "price_level", "serves_vegetarian", "editorial_summary",
    "rating", "user_rating_count", "open_now", "opening_hours",
    "enriched_at", "menu_fetched_at", "business_status",
)

# Cross-restaurant search keeps consumer-facing dish/classification evidence,
# but does not need internal model timestamps or a copy of the restaurant's
# address, coordinates, hours, and rating on each of tens of thousands of
# rows. DishExplore joins this compact index to restaurants.json by
# restaurant_id. Per-restaurant shards intentionally keep the original full
# rows for menu/detail surfaces.
_GLOBAL_DISH_FIELDS = (
    "id", "restaurant_id", "name", "raw_description", "price", "calories",
    "category", "verdict", "confidence", "reasoning", "dairy_status",
    "gluten_status", "nut_status", "protein_level", "serving_role",
    "meal_types", "key_ingredients", "alcohol_status", "up_votes",
    "down_votes", "menu_url",
    # Second-pass enrichment (dish_attributes.py): filters, badges, and the
    # "Make it vegan" line. Enum strings gzip to almost nothing at 60k rows.
    "vegetarian_status", "protein_source", "egg_status", "soy_status",
    "sesame_status", "spice_level", "cooking_method", "dish_format",
    "meat_sources", "flavor_profile", "ingredient_tags", "vegan_adaptation",
)


def _global_dish_rows(dishes: list[dict]) -> list[dict]:
    """Strip only repeated restaurant context from the global dish index."""
    return [
        {field: dish.get(field) for field in _GLOBAL_DISH_FIELDS if field in dish}
        for dish in dishes
    ]


def _attach_dish_ids(
    restaurants: list[dict], dishes: list[dict]
) -> list[dict]:
    """Add a compact dish-to-restaurant locator to already-loaded cards.

    Saved only has numeric dish ids before a menu is loaded. Keeping ids on
    each restaurant lets that page fetch the handful of relevant menu shards
    instead of parsing the global cross-restaurant index.
    """
    ids_by_restaurant: dict[int, list[int]] = {}
    for dish in dishes:
        restaurant_id = dish.get("restaurant_id")
        dish_id = dish.get("id")
        if restaurant_id is None or dish_id is None:
            continue
        ids_by_restaurant.setdefault(int(restaurant_id), []).append(int(dish_id))
    return [
        {
            **restaurant,
            "dish_ids": ids_by_restaurant.get(int(restaurant["id"]), []),
        }
        for restaurant in restaurants
    ]


def _legacy_dish_gzip_path() -> Path:
    return DATA_DIR / LEGACY_DISH_GZIP_NAME


def _compact_dish_gzip_path() -> Path:
    return DATA_DIR / COMPACT_DISH_GZIP_NAME


def _write_data_manifest(published_at: str, dish_count: int) -> None:
    """Write the tiny no-cache lookup used to version immutable-ish assets."""
    _write_json_snapshot(
        DATA_DIR / "manifest.json",
        {
            "published_at": published_at,
            "data_version": published_at,
            "dishes_version": published_at,
            "dish_schema_version": 2,
            "dishes_asset": COMPACT_DISH_GZIP_NAME,
            "dish_count": dish_count,
        },
    )


def _write_compact_dish_snapshot(payload: dict) -> None:
    """Write the local plain catalog and the deployable compact-v2 gzip.

    Deliberately do not touch ``dishes.json.gz``: old cached frontend bundles
    still depend on that full, restaurant-context-bearing response.
    """
    _write_json_snapshot(DATA_DIR / "dishes.json", payload)
    _write_gzip_snapshot(_compact_dish_gzip_path(), payload)


def _snapshot_count_matches(snapshot: dict, rows: list, label: str) -> None:
    if "count" not in snapshot:
        return
    try:
        expected = int(snapshot["count"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} has an invalid count.") from exc
    if expected != len(rows):
        raise RuntimeError(
            f"{label} count is {expected}, but it contains {len(rows)} rows."
        )


def _row_id(row: dict, field: str, label: str) -> int:
    value = row.get(field)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label} has an invalid {field}: {value!r}.") from exc


def _validate_published_snapshot_parity(
    restaurant_snapshot: dict, dish_snapshot: dict
) -> None:
    """Fail unless directory, legacy catalog, and shards are an exact set.

    Compact rows cannot stand alone when a menu shard or restaurant directory
    row is missing. Validate the complete currently-published snapshot before
    any destructive/local writes so a partial export cannot be migrated into
    an unrecoverable deployment.
    """
    restaurants = restaurant_snapshot.get("restaurants") or []
    dishes = dish_snapshot.get("dishes") or []
    if not isinstance(restaurants, list) or not isinstance(dishes, list):
        raise RuntimeError("Published restaurant and dish snapshots must be lists.")
    _snapshot_count_matches(restaurant_snapshot, restaurants, "restaurants.json")
    _snapshot_count_matches(dish_snapshot, dishes, LEGACY_DISH_GZIP_NAME)

    restaurant_ids = [
        _row_id(row, "id", "Restaurant row") for row in restaurants
    ]
    if len(set(restaurant_ids)) != len(restaurant_ids):
        raise RuntimeError("restaurants.json contains duplicate restaurant ids.")
    restaurant_id_set = set(restaurant_ids)

    dishes_by_restaurant: dict[int, dict[int, dict]] = {}
    seen_dish_ids: set[int] = set()
    for row in dishes:
        dish_id = _row_id(row, "id", "Legacy dish row")
        restaurant_id = _row_id(
            row, "restaurant_id", f"Legacy dish {dish_id}"
        )
        if dish_id in seen_dish_ids:
            raise RuntimeError(
                f"{LEGACY_DISH_GZIP_NAME} contains duplicate dish id {dish_id}."
            )
        seen_dish_ids.add(dish_id)
        if restaurant_id not in restaurant_id_set:
            raise RuntimeError(
                f"Legacy dish {dish_id} references unpublished restaurant "
                f"{restaurant_id}."
            )
        dishes_by_restaurant.setdefault(restaurant_id, {})[dish_id] = row

    if set(dishes_by_restaurant) != restaurant_id_set:
        missing = sorted(restaurant_id_set - set(dishes_by_restaurant))
        raise RuntimeError(
            "Published restaurant/dish parity failed; restaurants without "
            f"legacy dishes: {missing[:10]}."
        )

    shard_paths = list(RESTAURANT_DISH_DIR.glob("*.json"))
    shard_ids: set[int] = set()
    shards_by_id: dict[int, Path] = {}
    for path in shard_paths:
        try:
            restaurant_id = int(path.stem)
        except ValueError as exc:
            raise RuntimeError(f"Unexpected menu shard filename: {path.name}.") from exc
        if restaurant_id in shard_ids:
            raise RuntimeError(f"Duplicate menu shard for restaurant {restaurant_id}.")
        shard_ids.add(restaurant_id)
        shards_by_id[restaurant_id] = path

    if shard_ids != restaurant_id_set:
        missing = sorted(restaurant_id_set - shard_ids)
        extra = sorted(shard_ids - restaurant_id_set)
        raise RuntimeError(
            "Published restaurant/shard parity failed; "
            f"missing shards={missing[:10]}, extra shards={extra[:10]}."
        )

    for restaurant_id in sorted(restaurant_id_set):
        path = shards_by_id[restaurant_id]
        try:
            shard_snapshot = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read menu shard {path.name}.") from exc
        shard_dishes = shard_snapshot.get("dishes") or []
        if not isinstance(shard_dishes, list):
            raise RuntimeError(f"Menu shard {path.name} dishes must be a list.")
        _snapshot_count_matches(shard_snapshot, shard_dishes, path.name)

        shard_by_id: dict[int, dict] = {}
        for row in shard_dishes:
            dish_id = _row_id(row, "id", f"Menu shard {path.name} row")
            row_restaurant_id = _row_id(
                row, "restaurant_id", f"Menu shard {path.name} dish {dish_id}"
            )
            if row_restaurant_id != restaurant_id:
                raise RuntimeError(
                    f"Menu shard {path.name} contains dish {dish_id} for "
                    f"restaurant {row_restaurant_id}."
                )
            if dish_id in shard_by_id:
                raise RuntimeError(
                    f"Menu shard {path.name} contains duplicate dish id {dish_id}."
                )
            shard_by_id[dish_id] = row

        legacy_by_id = dishes_by_restaurant[restaurant_id]
        if set(shard_by_id) != set(legacy_by_id):
            raise RuntimeError(
                f"Dish ids in menu shard {path.name} do not match the legacy catalog."
            )
        for dish_id, legacy_row in legacy_by_id.items():
            if shard_by_id[dish_id] != legacy_row:
                raise RuntimeError(
                    f"Dish {dish_id} in menu shard {path.name} does not exactly "
                    "match the legacy catalog."
                )


def compact_existing_snapshots() -> dict:
    """Compact the already-published catalog without exporting local backlog.

    This is intentionally separate from ``export``. It lets a data-format
    migration keep the exact restaurant and dish set that is live today,
    which matters when the local database also contains newly scraped rows
    that have not been reviewed for publication yet. Full per-restaurant
    shards are left untouched because their detail fields are still needed.
    """
    restaurants_path = DATA_DIR / "restaurants.json"
    dishes_path = DATA_DIR / "dishes.json"
    legacy_gzip_path = _legacy_dish_gzip_path()
    if not restaurants_path.exists():
        raise RuntimeError("restaurants.json does not exist; run a full export first.")
    if not legacy_gzip_path.exists():
        raise RuntimeError(
            f"{LEGACY_DISH_GZIP_NAME} does not exist; migration needs the "
            "tracked live catalog."
        )
    if _compact_dish_gzip_path().exists():
        raise RuntimeError(
            f"{COMPACT_DISH_GZIP_NAME} already exists. Refusing to rebuild it "
            "from the frozen legacy catalog and potentially roll back newer "
            "targeted publishes."
        )

    restaurant_snapshot = json.loads(restaurants_path.read_text(encoding="utf-8"))
    try:
        dish_snapshot = json.loads(gzip.decompress(legacy_gzip_path.read_bytes()))
    except (OSError, gzip.BadGzipFile, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not read tracked {LEGACY_DISH_GZIP_NAME}."
        ) from exc

    # This reads every shard but writes nothing. The legacy gzip remains the
    # rollback source even after a successful migration.
    _validate_published_snapshot_parity(restaurant_snapshot, dish_snapshot)

    published_rows = restaurant_snapshot.get("restaurants") or []
    published_dishes = _global_dish_rows(dish_snapshot.get("dishes") or [])
    published_rows = _attach_dish_ids(published_rows, published_dishes)
    published_at = datetime.now(timezone.utc).isoformat()

    _write_json_snapshot(
        restaurants_path,
        {
            "count": len(published_rows),
            "restaurants": published_rows,
            "published_at": published_at,
        },
    )
    _write_compact_dish_snapshot(
        {
            "count": len(published_dishes),
            "dishes": published_dishes,
            "published_at": published_at,
        }
    )
    _write_data_manifest(published_at, len(published_dishes))
    return {
        "restaurants": len(published_rows),
        "dishes": len(published_dishes),
        "message": (
            f"Compacted the existing published snapshot: "
            f"{len(published_rows)} restaurants and {len(published_dishes)} dishes."
        ),
    }


def _write_restaurant_dish_shards(
    dishes: list[dict], published_at: str
) -> None:
    """Write one consumer menu file per restaurant and remove stale shards."""
    RESTAURANT_DISH_DIR.mkdir(parents=True, exist_ok=True)
    dishes_by_restaurant: dict[int, list[dict]] = {}
    for dish in dishes:
        dishes_by_restaurant.setdefault(int(dish["restaurant_id"]), []).append(dish)

    shard_names = set()
    for restaurant_id, restaurant_dishes in dishes_by_restaurant.items():
        shard_name = f"{restaurant_id}.json"
        shard_names.add(shard_name)
        (RESTAURANT_DISH_DIR / shard_name).write_text(
            json.dumps(
                {
                    "count": len(restaurant_dishes),
                    "dishes": restaurant_dishes,
                    "published_at": published_at,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    for stale_path in RESTAURANT_DISH_DIR.glob("*.json"):
        if stale_path.name not in shard_names:
            stale_path.unlink()


def _write_restaurant_dish_shard(
    restaurant_id: int, dishes: list[dict], published_at: str
) -> None:
    """Update one existing consumer menu without touching other shards."""
    RESTAURANT_DISH_DIR.mkdir(parents=True, exist_ok=True)
    _write_json_snapshot(
        RESTAURANT_DISH_DIR / f"{restaurant_id}.json",
        {"count": len(dishes), "dishes": dishes, "published_at": published_at},
    )


def _json_bytes(payload: dict) -> bytes:
    """Compact UTF-8 JSON shared by plain and pre-compressed snapshots."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_gzip_snapshot(path: Path, payload: dict) -> None:
    """Write a deterministic pre-compressed JSON snapshot."""
    path.write_bytes(gzip.compress(_json_bytes(payload), compresslevel=9, mtime=0))


def _write_json_snapshot(path: Path, payload: dict, *, gzip_copy: bool = False) -> None:
    """Write a compact snapshot and, when requested, a deterministic .gz copy."""
    content = _json_bytes(payload)
    path.write_bytes(content)
    if gzip_copy:
        _write_gzip_snapshot(path.with_suffix(path.suffix + ".gz"), payload)


def _consumer_restaurant_row(restaurant: dict, counts: dict | None) -> dict:
    row = {field: restaurant.get(field) for field in _RESTAURANT_FIELDS}
    row["dish_count"] = counts["total"] if counts else 0
    row["vegan_options"] = counts["vegan_meals"] if counts else 0
    row["vegan_sides"] = counts["vegan_sides"] if counts else 0
    menu_source = db.get_menu_text(restaurant["id"])
    score = compute_vegan_score(
        vegan_meals=counts["vegan_meals"] if counts else 0,
        vegan_sides=counts["vegan_sides"] if counts else 0,
        substance_points=counts.get("vegan_substance_points", 0.0) if counts else 0.0,
        google_rating=restaurant.get("rating"),
        rating_count=restaurant.get("user_rating_count"),
        dessert_venue=restaurant.get("primary_type") in db.DESSERT_VENUE_TYPES,
        plant_protein_menu=menu_offers_plant_protein(
            menu_source["content"] if menu_source else None
        ),
    )
    row["vegan_score"] = score["score"]
    row["vegan_score_parts"] = score
    return row


def export() -> dict:
    db.init_db()
    counts = db.verdict_counts_by_restaurant()
    restaurants = []
    for r in db.list_restaurants():
        c = counts.get(r["id"])
        if not is_consumer_ready(r, c["total"] if c else 0):
            continue
        restaurants.append(_consumer_restaurant_row(r, c))

    dishes = [
        d
        for d in db.list_all_dishes()
        if is_consumer_food_venue(d) and d.get("verdict")
    ]
    restaurants = _attach_dish_ids(restaurants, dishes)
    global_dishes = _global_dish_rows(dishes)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    published_at = datetime.now(timezone.utc).isoformat()
    _write_json_snapshot(
        DATA_DIR / "restaurants.json",
        {"count": len(restaurants), "restaurants": restaurants,
         "published_at": published_at},
    )
    _write_compact_dish_snapshot(
        {"count": len(global_dishes), "dishes": global_dishes,
         "published_at": published_at}
    )
    _write_data_manifest(published_at, len(global_dishes))

    # Restaurant cards and map popups open one menu at a time. Small shards
    # keep that common action from downloading the full cross-menu search
    # index (currently tens of megabytes on the public site).
    _write_restaurant_dish_shards(dishes, published_at)
    return {"restaurants": len(restaurants), "dishes": len(dishes)}


def export_restaurant(restaurant_id: int) -> dict:
    """Refresh one already-published restaurant without exporting local backlog.

    Deep-dive repairs often need to reach production immediately while other
    newly scraped restaurants are still awaiting review. This preserves the
    published restaurant set and replaces only the selected row and dishes.
    """
    db.init_db()
    restaurants_path = DATA_DIR / "restaurants.json"
    dishes_path = DATA_DIR / "dishes.json"
    if not restaurants_path.exists():
        raise RuntimeError("Static snapshots do not exist; run a full export first.")

    restaurant_snapshot = json.loads(restaurants_path.read_text(encoding="utf-8"))
    if dishes_path.exists():
        dish_snapshot = json.loads(dishes_path.read_text(encoding="utf-8"))
    elif _compact_dish_gzip_path().exists():
        dish_snapshot = json.loads(
            gzip.decompress(_compact_dish_gzip_path().read_bytes())
        )
    elif _legacy_dish_gzip_path().exists():
        dish_snapshot = json.loads(
            gzip.decompress(_legacy_dish_gzip_path().read_bytes())
        )
    else:
        raise RuntimeError("Static dish snapshots do not exist; run a full export first.")
    published_rows = restaurant_snapshot.get("restaurants") or []
    if not any(row.get("id") == restaurant_id for row in published_rows):
        raise RuntimeError(
            f"Restaurant {restaurant_id} is not already published; use a full export."
        )

    restaurant = next(
        (row for row in db.list_restaurants() if row["id"] == restaurant_id),
        None,
    )
    if restaurant is None:
        raise RuntimeError(f"Restaurant {restaurant_id} was not found locally.")
    counts = db.verdict_counts_by_restaurant().get(restaurant_id)
    if not is_consumer_ready(restaurant, counts["total"] if counts else 0):
        raise RuntimeError(f"Restaurant {restaurant_id} is not consumer-ready.")

    replacement = _consumer_restaurant_row(restaurant, counts)
    published_rows = [
        replacement if row.get("id") == restaurant_id else row
        for row in published_rows
    ]
    target_dishes = [
        dish
        for dish in db.list_all_dishes()
        if dish.get("restaurant_id") == restaurant_id
        and is_consumer_food_venue(dish)
        and dish.get("verdict")
    ]
    previous_dishes = dish_snapshot.get("dishes") or []
    target_indexes = [
        index
        for index, dish in enumerate(previous_dishes)
        if dish.get("restaurant_id") == restaurant_id
    ]
    insert_at = target_indexes[0] if target_indexes else len(previous_dishes)
    published_dishes = [
        dish
        for dish in previous_dishes
        if dish.get("restaurant_id") != restaurant_id
    ]
    target_dishes.sort(
        key=lambda dish: (
            str(dish.get("name") or "").casefold(),
            str(dish.get("restaurant_name") or "").casefold(),
        )
    )
    published_dishes[insert_at:insert_at] = _global_dish_rows(target_dishes)
    published_dishes = _global_dish_rows(published_dishes)
    published_rows = _attach_dish_ids(published_rows, published_dishes)

    published_at = datetime.now(timezone.utc).isoformat()
    _write_json_snapshot(
        restaurants_path,
        {
            "count": len(published_rows),
            "restaurants": published_rows,
            "published_at": published_at,
        },
    )
    _write_compact_dish_snapshot(
        {
            "count": len(published_dishes),
            "dishes": published_dishes,
            "published_at": published_at,
        }
    )
    _write_data_manifest(published_at, len(published_dishes))
    _write_restaurant_dish_shard(restaurant_id, target_dishes, published_at)
    return {
        "restaurants": len(published_rows),
        "dishes": len(published_dishes),
        "restaurant_id": restaurant_id,
        "restaurant_dishes": len(target_dishes),
    }


def publish(push: bool = False) -> dict:
    """Export snapshots; optionally commit + push them (Pages redeploys).

    Only the data directory is staged, so uncommitted code changes are never
    swept into a publish. Callable from the CLI and the Admin publish button
    (POST /api/publish). Raises RuntimeError when the push itself fails.
    """
    summary = export()
    summary["pushed"] = False
    summary["message"] = (
        f"Exported {summary['restaurants']} restaurants and "
        f"{summary['dishes']} dishes."
    )
    if not push:
        return summary

    root = Path(__file__).parent
    subprocess.run(
        ["git", "add", str(DATA_DIR)],
        cwd=root, capture_output=True, text=True, check=True,
    )
    commit = subprocess.run(
        ["git", "commit", "-m", "Publish site data"],
        cwd=root, capture_output=True, text=True,
    )
    if commit.returncode != 0:
        summary["message"] = "Live site already up to date — data unchanged."
        return summary
    pushed = subprocess.run(
        ["git", "push"], cwd=root, capture_output=True, text=True, timeout=180,
    )
    if pushed.returncode != 0:
        raise RuntimeError(
            "git push failed: "
            + ((pushed.stderr or pushed.stdout or "").strip()[-400:])
        )
    summary["pushed"] = True
    summary["message"] = (
        f"Published {summary['restaurants']} restaurants / "
        f"{summary['dishes']} dishes — the live site redeploys in ~2 minutes."
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish static site data.")
    parser.add_argument(
        "--push", action="store_true",
        help="Commit the exported data and push (triggers the Pages deploy).",
    )
    parser.add_argument(
        "--compact-existing", action="store_true",
        help=(
            "Compact the current snapshots without exporting unreviewed "
            "restaurants from the local database."
        ),
    )
    args = parser.parse_args()
    if args.compact_existing:
        if args.push:
            parser.error("--compact-existing cannot be combined with --push")
        summary = compact_existing_snapshots()
    else:
        summary = publish(push=args.push)
    print(summary["message"])


if __name__ == "__main__":
    main()
