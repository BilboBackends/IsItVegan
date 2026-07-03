"""Local dashboard API for the VeganFind pipeline.

A thin JSON backend the React UI talks to, so you can see discovered
restaurants and trigger a discovery run from the browser. Read-only over the
SQLite DB except for the /discover endpoint, which runs Phase 0.

Run:
    python api.py            # serves http://localhost:5000

The Vite dev server proxies /api/* here (see frontend/vite.config.js), so the
frontend only ever talks to our own backend — no keys client-side.
"""
from __future__ import annotations

from flask import Flask, jsonify, request
from flask_cors import CORS

import db
import discover
import enrich
import ingest
from config import settings
from menu_score import score_menu_text
from venue_filter import is_consumer_food_venue

app = Flask(__name__)
# Allow the Vite dev server origin during local development.
CORS(app)


@app.get("/api/health")
def health() -> object:
    return jsonify({"status": "ok"})


@app.get("/api/config")
def get_config() -> object:
    """Non-secret discovery settings, so the UI can show what area it targets.

    Never returns the API key.
    """
    return jsonify(
        {
            "city": settings.discovery_city,
            "lat": settings.discovery_lat,
            "lng": settings.discovery_lng,
            "radius_meters": settings.discovery_radius_meters,
            "cell_radius_meters": settings.discovery_cell_radius_meters,
            "has_api_key": bool(settings.google_places_api_key),
            "database_path": settings.database_path,
        }
    )


@app.get("/api/restaurants")
def get_restaurants() -> object:
    db.init_db()
    restaurants = db.list_restaurants()
    include_excluded = request.args.get("include_excluded") == "true"
    for restaurant in restaurants:
        restaurant["is_consumer_venue"] = is_consumer_food_venue(restaurant)
    if not include_excluded:
        restaurants = [r for r in restaurants if r["is_consumer_venue"]]
    counts = db.verdict_counts_by_restaurant()
    veganish = ("vegan", "likely_vegan", "vegan_adaptable")
    for r in restaurants:
        c = counts.get(r["id"])
        r["dish_count"] = c["total"] if c else 0
        r["vegan_options"] = (
            sum(c["by_verdict"].get(v, 0) for v in veganish) if c else 0
        )
    return jsonify({"count": len(restaurants), "restaurants": restaurants})


@app.post("/api/discover")
def run_discovery() -> object:
    """Trigger a Phase 0 discovery run and persist results.

    Synchronous — a run is ~49 Places calls (~20s). Fine for a local tool.
    Pass {"dry_run": true} to preview without writing to the DB.
    """
    if not settings.google_places_api_key:
        return (
            jsonify(
                {
                    "error": "GOOGLE_PLACES_API_KEY is not set. Add it to .env "
                    "and restart the server."
                }
            ),
            400,
        )

    dry_run = bool((request.get_json(silent=True) or {}).get("dry_run"))
    try:
        found = discover.run(dry_run=dry_run)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    return jsonify(
        {
            "dry_run": dry_run,
            "discovered": len(found),
            "total_in_db": db.count_restaurants(),
        }
    )


@app.post("/api/ingest")
def run_ingest() -> object:
    """Trigger Phase 1 menu-text ingestion.

    Scrapes websites for restaurants that don't have menu text yet, all of
    them with {"all": true}, or a single one with {"restaurant_id": N}.
    Synchronous; a stubborn ordering-platform site can take ~a minute.
    """
    payload = request.get_json(silent=True) or {}
    stale_days = payload.get("stale_days")
    if stale_days is not None and (
        not isinstance(stale_days, int) or isinstance(stale_days, bool) or stale_days < 1
    ):
        return jsonify({"error": "stale_days must be a positive integer."}), 400
    try:
        result = ingest.run(
            restaurant_id=payload.get("restaurant_id"),
            do_all=bool(payload.get("all")),
            stale_days=stale_days,
        )
    except SystemExit as exc:  # e.g. restaurant has no website
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(
        {
            "succeeded": result["succeeded"],
            "failed": result["failed"],
            "failures": [
                {"name": name, "error": err} for name, err in result["failures"]
            ],
        }
    )


@app.post("/api/enrich")
def run_enrich() -> object:
    """Trigger Google food-signal enrichment (servesVegetarianFood, etc.)."""
    if not settings.google_places_api_key:
        return jsonify({"error": "GOOGLE_PLACES_API_KEY is not set."}), 400
    payload = request.get_json(silent=True) or {}
    try:
        result = enrich.run(do_all=bool(payload.get("all")))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(result)


@app.post("/api/restaurants/add")
def add_restaurants_endpoint() -> object:
    """Add restaurants by name: resolve, enrich, ingest, and classify each.

    Body: {"names": ["...", ...]}. Synchronous — each added restaurant runs
    the full pipeline including Claude classification (~$0.10 and up to a
    couple of minutes per restaurant); fine for a handful of names in a
    local tool.
    """
    if not settings.google_places_api_key:
        return jsonify({"error": "GOOGLE_PLACES_API_KEY is not set."}), 400
    payload = request.get_json(silent=True) or {}
    names = [n.strip() for n in payload.get("names", []) if isinstance(n, str) and n.strip()]
    if not names:
        return jsonify({"error": "Provide names: [\"...\"]"}), 400
    if len(names) > 15:
        return jsonify({"error": "Max 15 names per request."}), 400
    try:
        import add_restaurants

        result = add_restaurants.run(names)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(result)


@app.patch("/api/restaurants/<int:restaurant_id>/visibility")
def update_restaurant_visibility(restaurant_id: int) -> object:
    db.init_db()
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("hidden"), bool):
        return jsonify({"error": "hidden must be true or false."}), 400
    if not db.set_restaurant_hidden(restaurant_id, payload["hidden"]):
        return jsonify({"error": "Restaurant not found."}), 404
    return jsonify({"id": restaurant_id, "hidden": payload["hidden"]})


@app.get("/api/restaurants/<int:restaurant_id>/dishes")
def restaurant_dishes(restaurant_id: int) -> object:
    """All dishes for a restaurant with their latest vegan verdicts."""
    dishes = db.list_dishes(restaurant_id)
    return jsonify({"count": len(dishes), "dishes": dishes})


@app.get("/api/dishes")
def all_dishes() -> object:
    """All classified menu items with their restaurant metadata."""
    db.init_db()
    dishes = db.list_all_dishes()
    dishes = [dish for dish in dishes if is_consumer_food_venue(dish)]
    return jsonify({"count": len(dishes), "dishes": dishes})


_REPORT_TYPES = {
    "animal_ingredient",
    "dish_removed",
    "wrong_restaurant",
    "other",
}


@app.post("/api/reports")
def create_report() -> object:
    db.init_db()
    payload = request.get_json(silent=True) or {}
    issue_type = payload.get("issue_type")
    restaurant_id = payload.get("restaurant_id")
    dish_id = payload.get("dish_id")
    note = str(payload.get("note") or "").strip()[:1000] or None
    if issue_type not in _REPORT_TYPES:
        return jsonify({"error": "Choose a valid issue type."}), 400
    if not isinstance(restaurant_id, int):
        return jsonify({"error": "restaurant_id is required."}), 400
    try:
        report_id = db.create_report(
            restaurant_id, issue_type, dish_id=dish_id, note=note
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"id": report_id, "status": "open"}), 201


@app.get("/api/reports")
def get_reports() -> object:
    db.init_db()
    status = request.args.get("status", "open")
    if status not in ("open", "resolved", "all"):
        return jsonify({"error": "Invalid status."}), 400
    reports = db.list_reports(None if status == "all" else status)
    return jsonify({"count": len(reports), "reports": reports})


@app.patch("/api/reports/<int:report_id>")
def update_report(report_id: int) -> object:
    db.init_db()
    payload = request.get_json(silent=True) or {}
    if payload.get("status") != "resolved":
        return jsonify({"error": "Only resolution is supported."}), 400
    if not db.resolve_report(report_id):
        return jsonify({"error": "Open report not found."}), 404
    return jsonify({"id": report_id, "status": "resolved"})


@app.post("/api/classify")
def run_classify() -> object:
    """Classify dishes for restaurants that have menu text but no dishes yet.

    Synchronous — fine when only a few restaurants remain; the initial bulk
    run should use the CLI (python classify.py).
    """
    if not settings.anthropic_api_key:
        return jsonify({"error": "ANTHROPIC_API_KEY is not set."}), 400
    payload = request.get_json(silent=True) or {}
    try:
        import classify

        result = classify.run(
            restaurant_id=payload.get("restaurant_id"),
        )
    except SystemExit as exc:  # e.g. restaurant has no menu text yet
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(result)


@app.get("/api/restaurants/<int:restaurant_id>/menu-text")
def restaurant_menu_text(restaurant_id: int) -> object:
    """Return the scraped menu text + its menu-likeness score, or 404."""
    source = db.get_menu_text(restaurant_id)
    if source is None:
        return jsonify({"error": "No menu text ingested for this restaurant."}), 404
    score = score_menu_text(source["content"])
    source["menu_score"] = score.score
    source["menu_score_reason"] = score.reason
    return jsonify(source)


if __name__ == "__main__":
    # Local only. debug=True gives auto-reload while iterating on the pipeline.
    app.run(host="127.0.0.1", port=5000, debug=True)
