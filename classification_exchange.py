"""Export/import menu classification jobs for an interactive chat model.

This is the manual counterpart to classification_providers.py: export stored
menu text to a JSON job, let an authenticated chat produce schema-shaped JSON,
then validate and persist it through the same domain model as API providers.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import db
from classifier import _SCHEMA, _SYSTEM, ClassificationResult, result_from_data


def export_job(restaurant_id: int) -> dict:
    db.init_db()
    restaurant = next(
        (row for row in db.list_restaurants() if row["id"] == restaurant_id), None
    )
    if restaurant is None:
        raise SystemExit(f"No restaurant with id {restaurant_id}.")
    source = db.get_menu_text(restaurant_id)
    if source is None:
        raise SystemExit(f"Restaurant {restaurant_id} has no menu text.")
    return {
        "job_version": 1,
        "restaurant": {
            "id": restaurant["id"],
            "name": restaurant["name"],
            "editorial_summary": restaurant.get("editorial_summary"),
            "serves_vegetarian": restaurant.get("serves_vegetarian"),
        },
        "instructions": _SYSTEM,
        "output_schema": _SCHEMA,
        "menu_text": source["content"],
    }


def import_result(
    restaurant_id: int,
    data: dict,
    *,
    provider: str = "codex_chat",
    model: str = "interactive-chat",
) -> dict:
    db.init_db()
    restaurant = next(
        (row for row in db.list_restaurants() if row["id"] == restaurant_id), None
    )
    if restaurant is None:
        raise SystemExit(f"No restaurant with id {restaurant_id}.")
    source = db.get_menu_text(restaurant_id)
    if source is None:
        raise SystemExit(f"Restaurant {restaurant_id} has no menu text.")
    result = result_from_data(
        data,
        provider=provider,
        model=model,
        billing="chatgpt_subscription",
    )
    if not result.ok:
        raise SystemExit(result.error or "No valid dishes in result file.")
    _persist(restaurant_id, source["id"], result)
    return {
        "restaurant_id": restaurant_id,
        "name": restaurant["name"],
        "dishes": len(result.dishes),
        "provider": provider,
    }


def _persist(
    restaurant_id: int, source_id: int, result: ClassificationResult
) -> None:
    classified_at = datetime.now(timezone.utc).isoformat()
    db.record_classify_cost(restaurant_id, None, provider=result.provider)
    db.delete_dishes_for_restaurant(restaurant_id)
    for dish in result.dishes:
        dish_id = db.upsert_dish(
            restaurant_id,
            dish.name,
            dish.description,
            dish.price,
            category=dish.category,
        )
        reasoning = dish.reasoning
        if dish.evidence:
            reasoning = f"{reasoning} | evidence: “{dish.evidence}”"
        db.insert_classification(
            dish_id=dish_id,
            verdict=dish.verdict,
            confidence=dish.confidence,
            reasoning=reasoning,
            source_id=source_id,
            model_version=result.model,
            created_at=classified_at,
            dairy_status=dish.dairy_status,
            gluten_status=dish.gluten_status,
            nut_status=dish.nut_status,
            protein_level=dish.protein_level,
            serving_role=dish.serving_role,
            meal_types=dish.meal_types,
            key_ingredients=dish.key_ingredients,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat classification exchange.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--restaurant-id", type=int, required=True)
    export_parser.add_argument("--output", type=Path, required=True)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--restaurant-id", type=int, required=True)
    import_parser.add_argument("--input", type=Path, required=True)
    import_parser.add_argument("--provider", default="codex_chat")
    import_parser.add_argument("--model", default="interactive-chat")
    args = parser.parse_args()

    if args.command == "export":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(export_job(args.restaurant_id), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Exported classification job to {args.output}")
        return
    data = json.loads(args.input.read_text(encoding="utf-8"))
    result = import_result(
        args.restaurant_id, data, provider=args.provider, model=args.model
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
