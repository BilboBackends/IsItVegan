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
    except Exception as exc:  # surface the failure to the UI instead of a 500 blob
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

    Scrapes websites for restaurants that don't have menu text yet (or all,
    with {"all": true}). Synchronous; can take a minute across many sites.
    """
    payload = request.get_json(silent=True) or {}
    try:
        result = ingest.run(do_all=bool(payload.get("all")))
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
