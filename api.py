"""Local dashboard API for the VeganFind pipeline.

The JSON backend the React UI talks to: consumer reads (restaurants, dishes,
menu versions, dish changes) plus the Admin pipeline controls — discovery,
enrichment, background scrape/classify jobs with live progress, add/archive/
hide restaurants, quality audit, provider usage. LOCAL ONLY: it is never
deployed; the public site is a static export (publish_static.py).

Run:
    python api.py            # serves http://localhost:5000

The Vite dev server proxies /api/* here (see frontend/vite.config.js), so the
frontend only ever talks to our own backend — no keys client-side.
"""
from __future__ import annotations

import gzip
import threading

from flask import Flask, jsonify, request
from flask_cors import CORS

import classifier
import db
import discover
import enrich
import ingest
import menu_audit
from classification_providers import ProviderUnavailable, provider_status, resolve_provider
from config import settings
from menu_score import score_menu_text
from venue_filter import is_consumer_food_venue

app = Flask(__name__)
# Allow the Vite dev server origin during local development.
CORS(app)


@app.after_request
def compress_dish_database(response):
    """Compress the large cross-restaurant read model for remote browsers.

    Restaurant metadata repeats across dishes, so this endpoint compresses
    especially well. A short private cache avoids re-downloading it while a
    user moves between Explore and Saved without hiding fresh classifications
    for long.
    """
    if request.path != "/api/dishes" or response.status_code != 200:
        return response

    response.cache_control.private = True
    response.cache_control.max_age = 30
    if "gzip" not in request.headers.get("Accept-Encoding", "").lower():
        return response
    if response.headers.get("Content-Encoding"):
        return response

    raw = response.get_data()
    if len(raw) < 1_024:
        return response
    compressed = gzip.compress(raw, compresslevel=5)
    if len(compressed) >= len(raw):
        return response

    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(compressed))
    response.vary.add("Accept-Encoding")
    return response


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
            "classifier": provider_status(),
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
    for r in restaurants:
        menu_source = db.get_menu_text(r["id"]) if r.get("has_menu_text") else None
        menu_score = (
            score_menu_text(menu_source["content"])
            if menu_source is not None
            else None
        )
        r["menu_score"] = menu_score.score if menu_score else None
        r["menu_score_reason"] = menu_score.reason if menu_score else None
        r["menu_score_is_menu"] = menu_score.is_menu if menu_score else None
        c = counts.get(r["id"])
        r["dish_count"] = c["total"] if c else 0
        # Strict standard: vegan verdicts, or likely_vegan at high
        # confidence — vegan_adaptable never counts toward a headline
        # number. Meals only; sides counted separately.
        r["vegan_options"] = c["vegan_meals"] if c else 0
        r["vegan_sides"] = c["vegan_sides"] if c else 0
        # Pre-run classification cost estimate from menu size; the actual
        # cost of the last run (if any) rides along as last_classify_cost.
        r["classify_estimate"] = (
            classifier.estimate_cost(r["menu_chars"])
            if r.get("has_menu_text") and r.get("menu_chars")
            else None
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


# Live progress for the one background bulk-ingest job at a time, polled by
# the Admin UI. Single-restaurant rescrapes stay synchronous (fast enough for
# a per-row button) and don't touch this state.
_ingest_state: dict = {
    "running": False,
    "total": None,
    "done": 0,
    "succeeded": 0,
    "failed": 0,
    "current": None,   # restaurant currently being scraped
    "recent": [],      # last few per-restaurant results, newest first
    "summary": None,   # {"succeeded": N, "failed": N} once finished
    "error": None,     # fatal job error (not per-restaurant failures)
}
_ingest_lock = threading.Lock()


def _ingest_worker(
    do_all: bool, stale_days: int | None, restaurant_ids: list[int] | None
) -> None:
    def on_progress(event: dict) -> None:
        with _ingest_lock:
            if "total" in event:
                _ingest_state["total"] = event["total"]
            if "current" in event:
                _ingest_state["current"] = event["current"]
            result = event.get("result")
            if result:
                _ingest_state["done"] += 1
                _ingest_state["succeeded" if result["ok"] else "failed"] += 1
                _ingest_state["recent"] = [result] + _ingest_state["recent"][:9]
                _ingest_state["current"] = None

    try:
        summary = ingest.run(
            do_all=do_all,
            stale_days=stale_days,
            restaurant_ids=restaurant_ids,
            on_progress=on_progress,
        )
        with _ingest_lock:
            _ingest_state["summary"] = {
                "succeeded": summary["succeeded"],
                "failed": summary["failed"],
            }
    except (Exception, SystemExit) as exc:
        with _ingest_lock:
            _ingest_state["error"] = str(exc)
    finally:
        with _ingest_lock:
            _ingest_state["running"] = False
            _ingest_state["current"] = None


@app.post("/api/ingest")
def run_ingest() -> object:
    """Trigger Phase 1 menu-text ingestion.

    With {"restaurant_id": N}: synchronous, returns that scrape's result.
    Bulk ({}, {"all": true}, or {"stale_days": N}): starts a background job
    and returns 202 immediately — poll GET /api/ingest/status for progress.
    """
    payload = request.get_json(silent=True) or {}
    stale_days = payload.get("stale_days")
    if stale_days is not None and (
        not isinstance(stale_days, int) or isinstance(stale_days, bool) or stale_days < 1
    ):
        return jsonify({"error": "stale_days must be a positive integer."}), 400

    restaurant_id = payload.get("restaurant_id")
    if restaurant_id is not None:
        try:
            result = ingest.run(restaurant_id=restaurant_id)
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

    restaurant_ids = payload.get("restaurant_ids")
    if restaurant_ids is not None and (
        not isinstance(restaurant_ids, list)
        or not restaurant_ids
        or len(restaurant_ids) > 100
        or any(not isinstance(value, int) or isinstance(value, bool) for value in restaurant_ids)
    ):
        return jsonify({"error": "restaurant_ids must be 1-100 integer IDs."}), 400
    if restaurant_ids is not None:
        restaurant_ids = list(dict.fromkeys(restaurant_ids))

    with _ingest_lock:
        if _ingest_state["running"]:
            return jsonify({"error": "A menu scrape is already running."}), 409
        _ingest_state.update(
            running=True, total=None, done=0, succeeded=0, failed=0,
            current=None, recent=[], summary=None, error=None,
        )
    threading.Thread(
        target=_ingest_worker,
        args=(bool(payload.get("all")), stale_days, restaurant_ids),
        daemon=True,
    ).start()
    return jsonify({"started": True}), 202


@app.get("/api/ingest/status")
def ingest_status() -> object:
    """Progress of the current (or last) bulk ingest job."""
    with _ingest_lock:
        return jsonify({**_ingest_state, "recent": list(_ingest_state["recent"])})


@app.get("/api/provider-usage")
def provider_usage() -> object:
    """Subscription usage windows for the claude/codex providers (cached)."""
    import usage_limits

    return jsonify(usage_limits.provider_usage())


@app.get("/api/menu-quality")
def menu_quality() -> object:
    """Automated audit of stored menus — flags likely-false/incomplete ones."""
    db.init_db()
    findings = menu_audit.audit_menus()
    return jsonify({
        "count": sum(not finding.get("review_status") for finding in findings),
        "known_issue_count": sum(
            finding.get("review_status") == "known_issue" for finding in findings
        ),
        "verified_count": sum(
            finding.get("review_status") == "verified" for finding in findings
        ),
        "findings": findings,
    })


@app.put("/api/menu-quality/<int:restaurant_id>/review")
def review_menu_quality(restaurant_id: int) -> object:
    """Acknowledge the current fingerprint as correct or a known issue."""
    db.init_db()
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    fingerprint = str(payload.get("fingerprint") or "")
    if status not in {"verified", "known_issue"}:
        return jsonify({"error": "status must be verified or known_issue"}), 400
    current = next(
        (
            finding
            for finding in menu_audit.audit_menus()
            if finding["restaurant_id"] == restaurant_id
        ),
        None,
    )
    if current is None:
        return jsonify({"error": "Current menu-quality finding not found."}), 404
    if not fingerprint or fingerprint != current["fingerprint"]:
        return jsonify({"error": "This warning changed; reload and review it again."}), 409
    db.set_menu_quality_review(
        restaurant_id,
        fingerprint=fingerprint,
        status=status,
        note=payload.get("note"),
    )
    return jsonify({"restaurant_id": restaurant_id, "status": status})


@app.delete("/api/menu-quality/<int:restaurant_id>/review")
def reopen_menu_quality(restaurant_id: int) -> object:
    """Remove a human disposition so the audit finding becomes active again."""
    db.init_db()
    if not db.clear_menu_quality_review(restaurant_id):
        return jsonify({"error": "Menu-quality review not found."}), 404
    return jsonify({"restaurant_id": restaurant_id, "status": "active"})


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


@app.post("/api/prospect")
def prospect_endpoint() -> object:
    """Area prospecting for the Admin prospect view — NO writes.

    Body: {"query": "restaurants on Mills Ave Orlando"}. Returns every place
    Google Text Search finds (up to ~60), flagged with already_added_id /
    archived when the place is in the DB, so the human picks what enters
    the pipeline via POST /api/restaurants/add.
    """
    if not settings.google_places_api_key:
        return jsonify({"error": "GOOGLE_PLACES_API_KEY is not set."}), 400
    payload = request.get_json(silent=True) or {}
    query = (payload.get("query") or "").strip()
    if not query or len(query) > 200:
        return jsonify({"error": "Provide a search query (max 200 chars)."}), 400
    from places_client import prospect_places

    db.init_db()
    try:
        places = prospect_places(
            query,
            api_key=settings.google_places_api_key,
            bias_lat=settings.discovery_lat,
            bias_lng=settings.discovery_lng,
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    existing = {r["place_id"]: r for r in db.list_restaurants()}
    for place in places:
        known = existing.get(place["place_id"])
        place["already_added_id"] = known["id"] if known else None
        place["archived"] = bool(known and known.get("archived"))
    return jsonify({"count": len(places), "places": places})


@app.post("/api/restaurants/resolve")
def resolve_restaurants_endpoint() -> object:
    """Resolve names to selectable Places candidates — NO writes.

    Body: {"names": ["...", ...]}. The confirm step of the Admin add flow:
    the user picks the exact place per name before anything is added.
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

        return jsonify({"resolved": add_restaurants.resolve_candidates(names)})
    except SystemExit as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.post("/api/restaurants/add")
def add_restaurants_endpoint() -> object:
    """Add user-confirmed places and run the CHOSEN pipeline stages.

    Body: {"places": [<candidate from /resolve>, ...],
           "ingest": bool, "classify": bool}
    Enrichment always runs; scraping and classification are opt-in.
    Synchronous — up to a couple of minutes per place with both enabled.
    (Legacy: {"names": [...]} still resolves-and-adds unattended.)
    """
    if not settings.google_places_api_key:
        return jsonify({"error": "GOOGLE_PLACES_API_KEY is not set."}), 400
    payload = request.get_json(silent=True) or {}

    try:
        import add_restaurants

        places = payload.get("places")
        if places is not None:
            if (
                not isinstance(places, list)
                or not places
                or len(places) > 15
                or any(
                    not isinstance(p, dict) or not p.get("place_id") or not p.get("name")
                    for p in places
                )
            ):
                return jsonify({"error": "places must be 1-15 resolve candidates."}), 400
            result = add_restaurants.add_places(
                places,
                do_ingest=bool(payload.get("ingest", True)),
                do_classify=bool(payload.get("classify", True)),
            )
            return jsonify(result)

        names = [n.strip() for n in payload.get("names", []) if isinstance(n, str) and n.strip()]
        if not names:
            return jsonify({"error": "Provide places: [...] or names: [\"...\"]"}), 400
        if len(names) > 15:
            return jsonify({"error": "Max 15 names per request."}), 400
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


@app.patch("/api/restaurants/<int:restaurant_id>/archived")
def update_restaurant_archived(restaurant_id: int) -> object:
    """Archive/restore a listing. Archived rows keep their data but leave
    the Admin working set, consumer views, and bulk pipeline runs."""
    db.init_db()
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("archived"), bool):
        return jsonify({"error": "archived must be true or false."}), 400
    if not db.set_restaurant_archived(restaurant_id, payload["archived"]):
        return jsonify({"error": "Restaurant not found."}), 404
    return jsonify({"id": restaurant_id, "archived": payload["archived"]})


@app.patch("/api/restaurants/<int:restaurant_id>/refresh-enabled")
def update_restaurant_refresh_enabled(restaurant_id: int) -> object:
    db.init_db()
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("enabled"), bool):
        return jsonify({"error": "enabled must be true or false."}), 400
    if not db.set_restaurant_refresh_enabled(restaurant_id, payload["enabled"]):
        return jsonify({"error": "Restaurant not found."}), 404
    return jsonify({"id": restaurant_id, "refresh_enabled": payload["enabled"]})


@app.delete("/api/restaurants/<int:restaurant_id>")
def permanently_delete_restaurant(restaurant_id: int) -> object:
    """Permanently delete a restaurant after exact-name confirmation."""
    db.init_db()
    with _ingest_lock:
        if _ingest_state["running"]:
            return jsonify({"error": "Wait for the active menu scrape to finish."}), 409
    with _classify_lock:
        if _classify_state["running"]:
            return jsonify({"error": "Wait for the active classification to finish."}), 409

    payload = request.get_json(silent=True) or {}
    confirm_name = payload.get("confirm_name")
    if not isinstance(confirm_name, str) or not confirm_name:
        return jsonify({"error": "Type the restaurant name to confirm deletion."}), 400
    try:
        deleted = db.delete_restaurant(
            restaurant_id, expected_name=confirm_name
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if deleted is None:
        return jsonify({"error": "Restaurant not found."}), 404
    return jsonify({"deleted": deleted})


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


@app.post("/api/dish-votes")
def dish_vote_endpoint() -> object:
    """Record a thumbs up/down for a dish (local app only; the public static
    site keeps votes in the browser). client_id is an anonymous per-browser
    token: with one, a repeat vote replaces the caller's previous vote and
    vote=null withdraws it, so one visitor counts once per dish."""
    db.init_db()
    payload = request.get_json(silent=True) or {}
    dish_id = payload.get("dish_id")
    vote = payload.get("vote")
    client_id = payload.get("client_id")
    if not isinstance(dish_id, int) or isinstance(dish_id, bool):
        return jsonify({"error": "dish_id must be an integer."}), 400
    if client_id is not None and (
        not isinstance(client_id, str)
        or not client_id.strip()
        or len(client_id) > 64
    ):
        return jsonify({"error": "client_id must be a short string."}), 400
    if vote is None:
        if not client_id:
            return jsonify({"error": "Withdrawing a vote needs a client_id."}), 400
    elif vote not in ("up", "down"):
        return jsonify({"error": "vote must be 'up', 'down', or null."}), 400
    if not db.record_dish_vote(dish_id, vote, client_id=client_id):
        return jsonify({"error": "Dish not found."}), 404
    return jsonify({"ok": True})


@app.post("/api/restaurant-votes")
def restaurant_vote_endpoint() -> object:
    """Thumbs up/down on a restaurant — same contract as /api/dish-votes."""
    db.init_db()
    payload = request.get_json(silent=True) or {}
    restaurant_id = payload.get("restaurant_id")
    vote = payload.get("vote")
    client_id = payload.get("client_id")
    if not isinstance(restaurant_id, int) or isinstance(restaurant_id, bool):
        return jsonify({"error": "restaurant_id must be an integer."}), 400
    if client_id is not None and (
        not isinstance(client_id, str)
        or not client_id.strip()
        or len(client_id) > 64
    ):
        return jsonify({"error": "client_id must be a short string."}), 400
    if vote is None:
        if not client_id:
            return jsonify({"error": "Withdrawing a vote needs a client_id."}), 400
    elif vote not in ("up", "down"):
        return jsonify({"error": "vote must be 'up', 'down', or null."}), 400
    if not db.record_restaurant_vote(restaurant_id, vote, client_id=client_id):
        return jsonify({"error": "Restaurant not found."}), 404
    return jsonify({"ok": True})


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


# Live progress for the one background classification job at a time. Both
# single-row and batch runs use this state so the Admin page can reconnect
# after a browser refresh without interrupting the model request.
_classify_state: dict = {
    "running": False,
    "cancel_requested": False,
    "total": None,
    "done": 0,
    "succeeded": 0,
    "failed": 0,
    "cost": 0.0,       # cumulative estimated API cost so far ($)
    "current": None,
    "recent": [],      # newest first; ok results carry dishes + cost
    "summary": None,   # {"ok", "failed", "dishes", "cost"} once finished
    "error": None,
    "provider": None,
    "billing": None,
}
_classify_lock = threading.Lock()
_classify_cancel = threading.Event()


def _classify_worker(
    do_all: bool,
    restaurant_ids: list[int] | None,
    provider: str | None,
    restaurant_id: int | None = None,
    parallel: int = 3,
    mode: str = "auto",
) -> None:
    import classify

    def on_progress(event: dict) -> None:
        with _classify_lock:
            if "total" in event:
                _classify_state["total"] = event["total"]
            if "current" in event:
                _classify_state["current"] = event["current"]
            result = event.get("result")
            if result:
                _classify_state["done"] += 1
                _classify_state["succeeded" if result["ok"] else "failed"] += 1
                _classify_state["cost"] = round(
                    _classify_state["cost"] + (result.get("cost") or 0.0), 3
                )
                _classify_state["recent"] = [result] + _classify_state["recent"][:9]
                _classify_state["current"] = None
                if result.get("provider"):
                    _classify_state["provider"] = result["provider"]
                    _classify_state["billing"] = result.get("billing")

    try:
        summary = classify.run(
            restaurant_id=restaurant_id,
            do_all=do_all,
            restaurant_ids=restaurant_ids,
            on_progress=on_progress,
            should_stop=_classify_cancel.is_set,
            provider=provider,
            parallel=parallel,
            mode=mode,
        )
        with _classify_lock:
            _classify_state["summary"] = {
                "ok": summary["ok"],
                "failed": summary["failed"],
                "dishes": summary["dishes"],
                "cost": summary["cost"],
                "cancelled": summary["cancelled"],
                "provider": _classify_state["provider"] or provider,
                "billing": _classify_state["billing"],
            }
    except (Exception, SystemExit) as exc:
        with _classify_lock:
            _classify_state["error"] = str(exc)
    finally:
        with _classify_lock:
            _classify_state["running"] = False
            _classify_state["current"] = None


@app.post("/api/classify")
def run_classify() -> object:
    """Classify dishes via the provider chain: Claude Code subscription,
    Codex/ChatGPT subscription, or the Anthropic API.

    Every run starts a background job, including {"restaurant_id": N}, so the
    browser can poll GET /api/classify/status and reconnect after a refresh.
    Bulk ({} classifies restaurants with menu text but no dishes yet;
    {"all": true} re-classifies everyone) uses the same job state.

    "provider" may be auto, a single provider, or a comma-separated priority
    list. The REQUESTED chain (not the resolved name) is what runs, so a
    provider that hits its usage limit mid-run fails over to the next one.
    """
    payload = request.get_json(silent=True) or {}
    requested_provider = payload.get("provider")
    try:
        # Validates the chain and that at least one provider can serve it;
        # the resolved name is for display until results report actual usage.
        provider = resolve_provider(requested_provider)
    except ProviderUnavailable as exc:
        return jsonify({"error": str(exc)}), 400

    restaurant_id = payload.get("restaurant_id")
    if restaurant_id is not None and (
        not isinstance(restaurant_id, int) or isinstance(restaurant_id, bool)
    ):
        return jsonify({"error": "restaurant_id must be an integer."}), 400

    restaurant_ids = None if restaurant_id is not None else payload.get("restaurant_ids")
    if restaurant_ids is not None and (
        not isinstance(restaurant_ids, list)
        or not restaurant_ids
        or len(restaurant_ids) > 100
        or any(not isinstance(value, int) or isinstance(value, bool) for value in restaurant_ids)
    ):
        return jsonify({"error": "restaurant_ids must be 1-100 integer IDs."}), 400
    if restaurant_ids is not None:
        restaurant_ids = list(dict.fromkeys(restaurant_ids))

    parallel = payload.get("parallel", 3)
    if not isinstance(parallel, int) or isinstance(parallel, bool) or not 1 <= parallel <= 6:
        return jsonify({"error": "parallel must be an integer from 1 to 6."}), 400

    mode = payload.get("mode", "auto")
    if mode not in ("auto", "full"):
        return jsonify({"error": "mode must be auto or full."}), 400

    with _classify_lock:
        if _classify_state["running"]:
            return jsonify({"error": "A classification run is already running."}), 409
        _classify_state.update(
            running=True, cancel_requested=False, total=None, done=0,
            succeeded=0, failed=0,
            cost=0.0, current=None, recent=[], summary=None, error=None,
            provider=provider,
            billing={
                "claude": "claude_subscription",
                "codex": "chatgpt_subscription",
            }.get(provider, "api"),
        )
        _classify_cancel.clear()
    threading.Thread(
        target=_classify_worker,
        args=(
            bool(payload.get("all")),
            restaurant_ids,
            requested_provider,
            restaurant_id,
            parallel,
            mode,
        ),
        daemon=True,
    ).start()
    return jsonify({"started": True, "provider": provider}), 202


@app.get("/api/restaurants/<int:restaurant_id>/menu-versions")
def restaurant_menu_versions(restaurant_id: int) -> object:
    """Distinct menu captures over time (newest first). ?full=1 for content."""
    db.init_db()
    include_content = request.args.get("full") == "1"
    versions = db.list_menu_versions(
        restaurant_id, include_content=include_content
    )
    return jsonify({"count": len(versions), "versions": versions})


@app.get("/api/restaurants/<int:restaurant_id>/dish-changes")
def restaurant_dish_changes(restaurant_id: int) -> object:
    """Dish-level menu drift: added/removed dishes, price/verdict changes."""
    db.init_db()
    changes = db.list_dish_changes(restaurant_id)
    return jsonify({"count": len(changes), "changes": changes})


@app.post("/api/classify/stop")
def stop_classify() -> object:
    """Request a safe stop after the currently active restaurant finishes."""
    with _classify_lock:
        if not _classify_state["running"]:
            return jsonify({"error": "No classification run is active."}), 409
        _classify_state["cancel_requested"] = True
        _classify_cancel.set()
    return jsonify({"stopping": True}), 202


@app.get("/api/classify/status")
def classify_status() -> object:
    """Progress + running cost of the current (or last) bulk classify job."""
    with _classify_lock:
        return jsonify({**_classify_state, "recent": list(_classify_state["recent"])})


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
