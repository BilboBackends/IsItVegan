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
import os
import threading

from flask import Flask, jsonify, request
from flask_cors import CORS

import activity
import classifier
import db
import discover
import enrich
import ingest
import menu_audit
from classification_providers import ProviderUnavailable, provider_status, resolve_provider
from config import settings
from location_filter import area_from_address, metro_from_area
from menu_score import score_menu_text
from vegan_score import compute_vegan_score, menu_offers_plant_protein
from venue_filter import is_consumer_food_venue, is_consumer_ready

app = Flask(__name__)
# Allow the Vite dev server origin during local development.
CORS(app)


# Admin restaurant summaries need two values derived from raw menu text: the
# explainable menu score and whether the menu advertises a plant protein.  A
# previous implementation reopened SQLite and rescanned the full text once
# per restaurant on every request.  Keep only the small derived values here,
# keyed by source metadata that changes whenever a menu snapshot changes.
_admin_menu_metric_cache: dict[tuple[str, int], tuple[tuple, dict]] = {}
_admin_menu_metric_lock = threading.Lock()


def _menu_metric_signature(restaurant: dict) -> tuple:
    return (
        restaurant.get("crawl_content_hash"),
        restaurant.get("menu_source_max_id"),
        restaurant.get("menu_source_count"),
        restaurant.get("menu_fetched_at"),
        restaurant.get("menu_chars"),
    )


def _admin_menu_metrics(
    restaurants: list[dict], db_path: str | None = None
) -> dict[int, dict]:
    """Return cached menu-derived Admin metrics, batch-reading cache misses."""
    path = os.path.abspath(db_path or settings.database_path)
    with _admin_menu_metric_lock:
        missing: list[dict] = []
        result: dict[int, dict] = {}
        for restaurant in restaurants:
            if not restaurant.get("has_menu_text"):
                continue
            cache_key = (path, restaurant["id"])
            cached = _admin_menu_metric_cache.get(cache_key)
            signature = _menu_metric_signature(restaurant)
            if cached is not None and cached[0] == signature:
                result[restaurant["id"]] = cached[1]
            else:
                missing.append(restaurant)

        # One connection (and at most two chunked SELECTs for very large
        # databases) replaces one connection per restaurant.
        menu_texts = (
            db.get_menu_texts(
                [restaurant["id"] for restaurant in missing], db_path=db_path
            )
            if missing
            else {}
        )
        for restaurant in missing:
            menu_source = menu_texts.get(restaurant["id"])
            if menu_source is None:
                metrics = {
                    "menu_score": None,
                    "menu_score_reason": None,
                    "menu_score_is_menu": None,
                    "plant_protein_menu": False,
                }
            else:
                content = menu_source["content"]
                menu_score = score_menu_text(content)
                metrics = {
                    "menu_score": menu_score.score,
                    "menu_score_reason": menu_score.reason,
                    "menu_score_is_menu": menu_score.is_menu,
                    "plant_protein_menu": menu_offers_plant_protein(content),
                }
            cache_key = (path, restaurant["id"])
            _admin_menu_metric_cache[cache_key] = (
                _menu_metric_signature(restaurant),
                metrics,
            )
            result[restaurant["id"]] = metrics
        return result


def _clear_admin_menu_metric_cache() -> None:
    """Test/server-maintenance hook; normal invalidation is fingerprinted."""
    with _admin_menu_metric_lock:
        _admin_menu_metric_cache.clear()


@app.after_request
def compress_dish_database(response):
    """Compress the large cross-restaurant read model for remote browsers.

    Restaurant metadata repeats across dishes, so this endpoint compresses
    especially well. A short private cache avoids re-downloading it while a
    user moves between Explore and Saved without hiding fresh classifications
    for long.
    """
    if (
        request.path not in ("/api/dishes", "/api/restaurants")
        or response.status_code != 200
    ):
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


@app.get("/api/admin/activity")
def get_admin_activity() -> object:
    """Recent user activity (sign-ups, notes, replies, votes, favorites,
    reports) from the Supabase user-data plane, read with the service role
    key. Local Admin only — this endpoint is never part of the static site.
    """
    if not activity.activity_config()["enabled"]:
        # Not an error: the feature is simply unconfigured. The UI shows
        # setup instructions for the listed variables.
        return jsonify(activity.fetch_activity())
    try:
        return jsonify(activity.fetch_activity())
    except Exception as exc:  # httpx errors, malformed payloads
        return jsonify({"error": f"Supabase request failed: {exc}"}), 502


@app.get("/api/restaurants")
def get_restaurants() -> object:
    db.init_db()
    restaurants = db.list_restaurants()
    include_excluded = request.args.get("include_excluded") == "true"
    # Estimates are priced against the provider that will actually run; the
    # UI passes its selected provider so the pre-run number matches the
    # button. Absent -> the configured default (DeepSeek here).
    estimate_provider = request.args.get("provider") or None
    for restaurant in restaurants:
        restaurant["is_consumer_venue"] = is_consumer_food_venue(restaurant)
        # City parsed from the Places address — the Admin coverage view
        # tracks the pipeline per area as coverage expands beyond Maitland.
        restaurant["area"] = area_from_address(restaurant.get("address"))
        restaurant["metro"] = metro_from_area(restaurant["area"])
    if not include_excluded:
        restaurants = [r for r in restaurants if r["is_consumer_venue"]]
    counts = db.verdict_counts_by_restaurant()
    menu_metrics = _admin_menu_metrics(restaurants)
    for r in restaurants:
        metrics = menu_metrics.get(r["id"], {})
        r["menu_score"] = metrics.get("menu_score")
        r["menu_score_reason"] = metrics.get("menu_score_reason")
        r["menu_score_is_menu"] = metrics.get("menu_score_is_menu")
        c = counts.get(r["id"])
        r["dish_count"] = c["total"] if c else 0
        # Strict standard: vegan verdicts, or likely_vegan at high
        # confidence — vegan_adaptable never counts toward a headline
        # number. Meals only; sides counted separately.
        r["vegan_options"] = c["vegan_meals"] if c else 0
        r["vegan_sides"] = c["vegan_sides"] if c else 0
        # One explainable 0-10 number for "how good is this place for a
        # vegan" — selection + substance + reputation (vegan_score.py).
        score = compute_vegan_score(
            vegan_meals=c["vegan_meals"] if c else 0,
            vegan_sides=c["vegan_sides"] if c else 0,
            substance_points=c.get("vegan_substance_points", 0.0) if c else 0.0,
            google_rating=r.get("rating"),
            rating_count=r.get("user_rating_count"),
            dessert_venue=r.get("primary_type") in db.DESSERT_VENUE_TYPES,
            plant_protein_menu=bool(metrics.get("plant_protein_menu")),
        )
        r["vegan_score"] = score["score"]
        r["vegan_score_parts"] = score
        # Pre-run classification cost estimate from menu size; the actual
        # cost of the last run (if any) rides along as last_classify_cost.
        r["classify_estimate"] = (
            classifier.estimate_cost(r["menu_chars"], provider=estimate_provider)
            if r.get("has_menu_text") and r.get("menu_chars")
            else None
        )
        # These fields are cache fingerprints, not part of the API contract.
        r.pop("crawl_content_hash", None)
        r.pop("crawl_menu_score", None)
        r.pop("crawl_last_success_at", None)
        r.pop("menu_source_count", None)
        r.pop("menu_source_max_id", None)
    if not include_excluded:
        restaurants = [
            restaurant
            for restaurant in restaurants
            if is_consumer_ready(restaurant, restaurant["dish_count"])
        ]
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
    "cancel_requested": False,
    # "started" once a then_classify chain launched, or the error string
    # explaining why it couldn't (e.g. a classify job was already running).
    "chained_classify": None,
}
_ingest_lock = threading.Lock()
# Atomically coordinates entry into ingest, classify, and Scrape Doctor jobs.
# Each job still owns its detailed state lock; this lock only closes the gap
# between "is another pipeline active?" and marking the new one active.
_pipeline_start_lock = threading.Lock()
# Set to request a graceful stop before the next restaurant. A scrape
# already in flight (a hung headless browser) is unstuck separately by
# terminating the orphaned browser processes — see stop_ingest.
_ingest_cancel = threading.Event()


def _scrape_doctor_running() -> bool:
    import scrape_doctor

    return bool(scrape_doctor.status().get("running"))


def _ingest_worker(
    do_all: bool,
    stale_days: int | None,
    restaurant_ids: list[int] | None,
    then_classify: dict | None = None,
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
            should_stop=_ingest_cancel.is_set,
        )
        with _ingest_lock:
            _ingest_state["summary"] = {
                "succeeded": summary["succeeded"],
                "failed": summary["failed"],
            }
        # Server-side chain: kick off classification for the same ids before
        # the scrape job reports finished, so the handoff can never be lost
        # to a closed tab (the original one-go-button failure mode).
        if then_classify is not None:
            error, _ = _start_classify_job(
                requested_provider=then_classify.get("provider"),
                restaurant_ids=restaurant_ids,
                parallel=int(then_classify.get("parallel", 3)),
                mode=then_classify.get("mode", "auto"),
            )
            with _ingest_lock:
                _ingest_state["chained_classify"] = error or "started"
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
        with _pipeline_start_lock:
            if _scrape_doctor_running():
                return jsonify(
                    {"error": "Wait for the active deep dive before scraping menus."}
                ), 409
            with _classify_lock:
                if _classify_state["running"]:
                    return jsonify(
                        {"error": "Wait for the active classification before scraping menus."}
                    ), 409
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
        or len(restaurant_ids) > 1000
        or any(not isinstance(value, int) or isinstance(value, bool) for value in restaurant_ids)
    ):
        return jsonify({"error": "restaurant_ids must be 1-1000 integer IDs."}), 400
    if restaurant_ids is not None:
        restaurant_ids = list(dict.fromkeys(restaurant_ids))

    # Optional server-side handoff: when the scrape finishes, start a
    # classification job for the same restaurant_ids. Validated up front so
    # a bad provider fails the request, not the chain an hour later.
    then_classify = payload.get("then_classify")
    if then_classify is not None:
        if not isinstance(then_classify, dict):
            return jsonify({"error": "then_classify must be an object."}), 400
        if restaurant_ids is None:
            return jsonify(
                {"error": "then_classify requires restaurant_ids."}
            ), 400
        try:
            resolve_provider(then_classify.get("provider"))
        except ProviderUnavailable as exc:
            return jsonify({"error": str(exc)}), 400
        chain_parallel = then_classify.get("parallel", 3)
        if (
            not isinstance(chain_parallel, int)
            or isinstance(chain_parallel, bool)
            or not 1 <= chain_parallel <= 6
        ):
            return jsonify({"error": "then_classify.parallel must be 1-6."}), 400
        if then_classify.get("mode", "auto") not in ("auto", "full"):
            return jsonify({"error": "then_classify.mode must be auto or full."}), 400

    with _pipeline_start_lock:
        if _scrape_doctor_running():
            return jsonify(
                {"error": "Wait for the active deep dive before scraping menus."}
            ), 409
        with _classify_lock:
            if _classify_state["running"]:
                return jsonify(
                    {"error": "Wait for the active classification before scraping menus."}
                ), 409
        with _ingest_lock:
            if _ingest_state["running"]:
                return jsonify({"error": "A menu scrape is already running."}), 409
            _ingest_state.update(
                running=True, total=None, done=0, succeeded=0, failed=0,
                current=None, recent=[], summary=None, error=None,
                cancel_requested=False, chained_classify=None,
            )
            _ingest_cancel.clear()
        threading.Thread(
            target=_ingest_worker,
            args=(bool(payload.get("all")), stale_days, restaurant_ids, then_classify),
            daemon=True,
        ).start()
    return jsonify({"started": True}), 202


def _kill_orphan_browsers() -> int:
    """Terminate headless browser processes left over from a hung scrape.

    A scrape can wedge on a headless page that never settles; the worker
    thread is then blocked inside the browser call and a graceful stop can't
    reach it. Killing the browser processes makes that call raise, so the
    thread unblocks, marks the restaurant failed, and honors the stop.

    Uses the platform's process killer targeting only browser image names —
    no extra dependency. This never targets the Python backend itself.
    """
    import subprocess

    # Playwright ships Chromium as headless_shell / chrome (chromium on
    # Linux). Kill by image name so only browser children die.
    if os.name == "nt":
        images = ("headless_shell.exe", "chrome.exe", "chromium.exe")
        killed = 0
        for image in images:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/IM", image],
                capture_output=True, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # taskkill prints one "SUCCESS" line per process terminated.
            killed += (result.stdout or "").upper().count("SUCCESS")
        return killed

    result = subprocess.run(
        ["pkill", "-f", "headless_shell|chromium|chrome"],
        capture_output=True, text=True,
    )
    return 0 if result.returncode not in (0, 1) else -1  # -1: count unknown


@app.post("/api/ingest/stop")
def stop_ingest() -> object:
    """Stop the running scrape. Requests a graceful halt before the next
    restaurant, AND terminates any orphaned headless-browser processes so a
    scrape wedged on a hung page unblocks instead of hanging forever. Menus
    already scraped are kept."""
    with _ingest_lock:
        if not _ingest_state["running"]:
            return jsonify({"error": "No menu scrape is active."}), 409
        _ingest_state["cancel_requested"] = True
        _ingest_cancel.set()
    try:
        killed = _kill_orphan_browsers()
    except Exception as exc:
        killed = None
        stop_note = f"browser cleanup failed: {exc}"
    else:
        stop_note = f"terminated {killed} browser process(es)"
    return jsonify({"stopping": True, "browsers": killed, "note": stop_note}), 202


@app.get("/api/ingest/status")
def ingest_status() -> object:
    """Progress of the current (or last) bulk ingest job."""
    with _ingest_lock:
        return jsonify({**_ingest_state, "recent": list(_ingest_state["recent"])})


@app.post("/api/scrape-fix")
def scrape_fix_endpoint() -> object:
    """Launch the Scrape Doctor through Claude Code or Codex.

    The selected subscription agent deep-dives one failed, incomplete, or
    incorrect scrape, fixes the scraper generically when needed, verifies,
    and commits code repairs but never pushes. A fixed or recovered result is
    then ingested and handed to DeepSeek classification server-side.
    One shared job at a time; poll /api/scrape-fix/status for the live log.
    """
    import scrape_doctor

    db.init_db()
    payload = request.get_json(silent=True) or {}
    restaurant_id = payload.get("restaurant_id")
    if not isinstance(restaurant_id, int) or isinstance(restaurant_id, bool):
        return jsonify({"error": "restaurant_id must be an integer."}), 400
    agent = payload.get("agent", "claude")
    if agent not in ("claude", "codex"):
        return jsonify({"error": "agent must be claude or codex."}), 400
    with _pipeline_start_lock:
        with _ingest_lock:
            if _ingest_state["running"]:
                return jsonify(
                    {"error": "Wait for the active menu scrape before starting a deep dive."}
                ), 409
        with _classify_lock:
            if _classify_state["running"]:
                return jsonify(
                    {"error": "Wait for the active classification before starting a deep dive."}
                ), 409
        try:
            return jsonify(
                scrape_doctor.start(
                    restaurant_id,
                    agent=agent,
                    on_fixed=_finish_scrape_doctor_pipeline,
                )
            )
        except LookupError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409


def _finish_scrape_doctor_pipeline(restaurant_id: int) -> str:
    """Persist the repaired menu, then start reconnectable DeepSeek work."""
    result = ingest.run(restaurant_id=restaurant_id)
    if result["succeeded"] != 1 or result["failed"]:
        detail = result.get("failures") or "scrape still failed"
        raise RuntimeError(f"re-scrape failed: {detail}")

    error, provider = _start_classify_job(
        requested_provider="deepseek",
        restaurant_id=restaurant_id,
        parallel=1,
        mode="auto",
    )
    if error:
        raise RuntimeError(f"DeepSeek classification was not started: {error}")
    provider_label = "DeepSeek" if provider == "deepseek" else (provider or "DeepSeek")
    return f"Menu stored successfully; {provider_label} classification started."


@app.get("/api/scrape-fix/status")
def scrape_fix_status() -> object:
    """Live state of the current (or last) Scrape Doctor run."""
    import scrape_doctor

    return jsonify(scrape_doctor.status())


@app.get("/api/scrape-failures")
def scrape_failures_endpoint() -> object:
    """Restaurants whose last scrape failed, with per-URL diagnostics —
    the Admin "why did this menu fail" panel."""
    db.init_db()
    failures = db.scrape_failures()
    return jsonify({"count": len(failures), "failures": failures})


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


@app.get("/api/dish-audit")
def dish_audit_findings() -> object:
    """Tier-0 dish audits: price/calorie/consistency sanity over stored data.

    Pure reads, zero LLM tokens — a full-DB sweep on demand. Returns active
    findings (dismissed ones filtered out unless include_dismissed=true).
    """
    import dish_audit

    db.init_db()
    include_dismissed = request.args.get("include_dismissed") == "true"
    findings = dish_audit.findings_as_dicts(
        dish_audit.audit_all(), include_dismissed=include_dismissed
    )
    from collections import Counter

    by_code = Counter(f["code"] for f in findings if not f["dismissed"])
    by_severity = Counter(f["severity"] for f in findings if not f["dismissed"])
    return jsonify(
        {
            "count": sum(not f["dismissed"] for f in findings),
            "by_code": dict(by_code),
            "by_severity": dict(by_severity),
            "findings": findings,
        }
    )


@app.post("/api/dish-audit/<int:dish_id>/dismiss")
def dismiss_dish_audit(dish_id: int) -> object:
    """Acknowledge one finding as reviewed-and-fine at its current value."""
    db.init_db()
    payload = request.get_json(silent=True) or {}
    code = str(payload.get("code") or "")
    fingerprint = str(payload.get("fingerprint") or "")
    if not code or not fingerprint:
        return jsonify({"error": "code and fingerprint are required."}), 400
    db.dismiss_dish_audit_finding(
        dish_id, code=code, fingerprint=fingerprint, note=payload.get("note")
    )
    return jsonify({"dish_id": dish_id, "code": code, "status": "dismissed"})


@app.post("/api/dish-audit/<int:dish_id>/apply")
def apply_dish_audit_fix(dish_id: int) -> object:
    """Apply an audit's one-click field correction (e.g. lost-decimal price).

    Body: {"field": "price"|"calories", "value": "<new value>"}. Verified
    against a fresh sweep so a stale UI can't write a value the audit no
    longer recommends.
    """
    import dish_audit

    db.init_db()
    payload = request.get_json(silent=True) or {}
    field = payload.get("field")
    value = payload.get("value")
    if field not in ("price", "calories") or not isinstance(value, str):
        return jsonify({"error": "field must be price/calories with a string value."}), 400
    # Confirm the audit currently suggests exactly this correction for this dish.
    match = next(
        (
            f
            for f in dish_audit.findings_as_dicts(dish_audit.audit_all())
            if f["dish_id"] == dish_id
            and f["field"] == field
            and f["suggested"] == value
        ),
        None,
    )
    if match is None:
        return jsonify({"error": "This correction is no longer suggested; reload."}), 409
    if not db.update_dish_field(dish_id, field=field, value=value):
        return jsonify({"error": "Dish not found."}), 404
    return jsonify({"dish_id": dish_id, "field": field, "value": value, "status": "applied"})


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


def _mark_existing_prospects(places: list[dict]) -> int:
    """Attach pipeline state to Places results; return the not-yet-added count."""
    existing = {r["place_id"]: r for r in db.list_restaurants()}
    new_count = 0
    for place in places:
        known = existing.get(place["place_id"])
        place["already_added_id"] = known["id"] if known else None
        place["archived"] = bool(known and known.get("archived"))
        if not known:
            new_count += 1
    return new_count


def _radius_sweep_inputs(payload: dict) -> tuple[float, float, float]:
    values = (payload.get("lat"), payload.get("lng"), payload.get("radius_meters"))
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in values
    ):
        raise ValueError("lat, lng, and radius_meters must be numbers.")
    return float(values[0]), float(values[1]), float(values[2])


@app.post("/api/prospect/radius/estimate")
def prospect_radius_estimate_endpoint() -> object:
    """Estimate tiled Nearby Search calls without contacting Google."""
    from places_client import estimate_radius_food_sweep

    payload = request.get_json(silent=True) or {}
    try:
        lat, lng, radius_meters = _radius_sweep_inputs(payload)
        estimate = estimate_radius_food_sweep(lat, lng, radius_meters)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(estimate)


@app.post("/api/prospect/radius")
def prospect_radius_endpoint() -> object:
    """Run a confirmed, tiled food-venue sweep inside an exact radius."""
    if not settings.google_places_api_key:
        return jsonify({"error": "GOOGLE_PLACES_API_KEY is not set."}), 400
    from places_client import radius_food_sweep

    payload = request.get_json(silent=True) or {}
    budget = payload.get("confirmed_call_budget")
    if isinstance(budget, bool) or not isinstance(budget, int):
        return jsonify({"error": "Estimate and confirm the call budget first."}), 400
    try:
        lat, lng, radius_meters = _radius_sweep_inputs(payload)
        result = radius_food_sweep(
            api_key=settings.google_places_api_key,
            lat=lat,
            lng=lng,
            radius_meters=radius_meters,
            confirmed_call_budget=budget,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    db.init_db()
    result["new_count"] = _mark_existing_prospects(result["places"])
    return jsonify(result)


# Category battery for the area sweep. Text Search tops out around 60
# results per query, so one "restaurants in Orlando" can never show the
# whole city — a spread of cuisine/venue queries pulls distinct slices and
# the union is deduped by place_id.
_SWEEP_QUERIES = (
    "restaurants",
    "vegan restaurants",
    "vegetarian restaurants",
    "breakfast and brunch restaurants",
    "pizza restaurants",
    "mexican restaurants",
    "chinese restaurants",
    "thai restaurants",
    "vietnamese restaurants",
    "indian restaurants",
    "sushi restaurants",
    "mediterranean restaurants",
    "cafes",
    "bakeries",
)


@app.post("/api/prospect/sweep")
def prospect_sweep_endpoint() -> object:
    """Coverage-gap sweep of one area — NO writes.

    Body: {"area": "Orlando, FL"}. Runs the _SWEEP_QUERIES battery through
    Text Search ("<category> in <area>"), merges unique places, and flags
    each with already_added_id like /api/prospect — "what am I missing in
    Orlando" becomes one click instead of a dozen manual searches. A broad
    pass, not a completeness guarantee (Google caps each query at ~60).
    """
    if not settings.google_places_api_key:
        return jsonify({"error": "GOOGLE_PLACES_API_KEY is not set."}), 400
    payload = request.get_json(silent=True) or {}
    area = (payload.get("area") or "").strip()
    if not area or len(area) > 120:
        return jsonify({"error": "Provide an area (max 120 chars)."}), 400
    from places_client import prospect_places

    db.init_db()
    merged: dict[str, dict] = {}
    queries_run = 0
    errors: list[str] = []
    for category in _SWEEP_QUERIES:
        try:
            places = prospect_places(
                f"{category} in {area}",
                api_key=settings.google_places_api_key,
                bias_lat=settings.discovery_lat,
                bias_lng=settings.discovery_lng,
            )
            queries_run += 1
        except Exception as exc:  # one failing category must not kill the sweep
            errors.append(f"{category}: {exc}")
            continue
        for place in places:
            merged.setdefault(place["place_id"], place)
    if not merged and errors:
        return jsonify({"error": "; ".join(errors[:3])}), 502

    existing = {r["place_id"]: r for r in db.list_restaurants()}
    out = sorted(merged.values(), key=lambda p: (p.get("name") or "").lower())
    for place in out:
        known = existing.get(place["place_id"])
        place["already_added_id"] = known["id"] if known else None
        place["archived"] = bool(known and known.get("archived"))
    new_count = sum(1 for p in out if not p["already_added_id"])
    return jsonify(
        {
            "count": len(out),
            "new_count": new_count,
            "queries_run": queries_run,
            "errors": errors,
            "places": out,
        }
    )


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
            do_ingest = bool(payload.get("ingest", True))
            do_classify = bool(payload.get("classify", True))
            # Synchronous scraping/classifying bounds the batch at 15; a
            # names-only add (enrichment only, ~1 cheap Places call each) can
            # take a whole Prospect page — scrape/classify then run as
            # background jobs scoped to the new ids.
            max_places = 15 if (do_ingest or do_classify) else 60
            if (
                not isinstance(places, list)
                or not places
                or len(places) > max_places
                or any(
                    not isinstance(p, dict) or not p.get("place_id") or not p.get("name")
                    for p in places
                )
            ):
                return jsonify(
                    {"error": f"places must be 1-{max_places} resolve candidates."}
                ), 400
            result = add_restaurants.add_places(
                places, do_ingest=do_ingest, do_classify=do_classify
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


@app.post("/api/publish")
def publish_site_endpoint() -> object:
    """Export consumer snapshots, commit, and push — updates the PUBLIC
    static site (GitHub Pages redeploys on push). Local Admin only."""
    import publish_static

    try:
        summary = publish_static.publish(push=True)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(summary)


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


def _start_classify_job(
    *,
    requested_provider: str | None,
    restaurant_ids: list[int] | None = None,
    parallel: int = 3,
    mode: str = "auto",
    do_all: bool = False,
    restaurant_id: int | None = None,
) -> tuple[str | None, str | None]:
    """Initialize the classify-job state and spawn its worker thread.

    Returns (error, resolved_provider); error is None on success. Shared by
    POST /api/classify and the ingest worker's then_classify chain, so a
    scrape can hand off to classification entirely server-side — the chain
    survives page navigation and browser closes.
    """
    try:
        provider = resolve_provider(requested_provider)
    except ProviderUnavailable as exc:
        return str(exc), None
    with _classify_lock:
        if _classify_state["running"]:
            return "A classification run is already running.", provider
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
        args=(do_all, restaurant_ids, requested_provider, restaurant_id,
              parallel, mode),
        daemon=True,
    ).start()
    return None, provider


@app.post("/api/classify")
def run_classify() -> object:
    """Classify dishes via the provider chain: Claude Code subscription,
    DeepSeek API.

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
        or len(restaurant_ids) > 1000
        or any(not isinstance(value, int) or isinstance(value, bool) for value in restaurant_ids)
    ):
        return jsonify({"error": "restaurant_ids must be 1-1000 integer IDs."}), 400
    if restaurant_ids is not None:
        restaurant_ids = list(dict.fromkeys(restaurant_ids))

    parallel = payload.get("parallel", 3)
    if not isinstance(parallel, int) or isinstance(parallel, bool) or not 1 <= parallel <= 6:
        return jsonify({"error": "parallel must be an integer from 1 to 6."}), 400

    mode = payload.get("mode", "auto")
    if mode not in ("auto", "full"):
        return jsonify({"error": "mode must be auto or full."}), 400

    with _pipeline_start_lock:
        if _scrape_doctor_running():
            return jsonify(
                {"error": "Wait for the active deep dive before classifying menus."}
            ), 409
        with _ingest_lock:
            if _ingest_state["running"]:
                return jsonify(
                    {"error": "Wait for the active menu scrape before classifying menus."}
                ), 409
        error, provider = _start_classify_job(
            requested_provider=requested_provider,
            restaurant_ids=restaurant_ids,
            parallel=parallel,
            mode=mode,
            do_all=bool(payload.get("all")),
            restaurant_id=restaurant_id,
        )
    if error:
        return jsonify({"error": error}), (
            409 if "already running" in error else 400
        )
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
    # Long-running scrape/classify/doctor jobs live in this process. Werkzeug's
    # file watcher would kill them as soon as an agent edits scraper code, so
    # keep debug tracebacks but require an explicit restart to load code edits.
    app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
