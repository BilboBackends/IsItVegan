"""Subscription usage/limits for the classification providers.

Shows the same numbers the CLIs' interactive /usage and /status screens show,
so the dashboard can answer "how much subscription do I have left?" before a
bulk classification run.

Claude: reads the LOCAL Claude Code OAuth token (read-only, never logged)
and asks the same endpoint the CLI's /usage screen uses. That endpoint is
UNDOCUMENTED — it can change with CLI updates — so every failure degrades to
{"available": False, "reason": ...} and never blocks classification.

Codex: not wired up yet. The CLI is not installed here and its auth/usage
surface is unverified; the slot exists so it can light up later without
touching the dashboard.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_OAUTH_BETA = "oauth-2025-04-20"

# One fetch per minute at most — the Admin page reloads data after every
# action and quota barely moves between clicks.
_CACHE_TTL_SECONDS = 60
_cache: dict = {"at": 0.0, "data": None}

_WINDOW_LABELS = {
    "five_hour": "5-hour session",
    "seven_day": "Week (all models)",
    "seven_day_sonnet": "Week (Sonnet)",
    "seven_day_opus": "Week (Opus)",
    "seven_day_oauth_apps": "Week (apps)",
}


def _credentials_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def _parse_windows(payload) -> list[dict]:
    """Tolerantly extract usage windows: any object with a numeric
    `utilization` counts. The endpoint is undocumented, so parse by shape,
    not by an assumed schema."""
    windows: list[dict] = []
    if not isinstance(payload, dict):
        return windows
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        utilization = value.get("utilization")
        if not isinstance(utilization, (int, float)):
            continue
        windows.append(
            {
                "id": key,
                "label": _WINDOW_LABELS.get(key, key.replace("_", " ")),
                "used_pct": max(0.0, min(100.0, float(utilization))),
                "resets_at": value.get("resets_at"),
            }
        )
    return windows


def _claude_usage() -> dict:
    path = _credentials_path()
    if not path.exists():
        return {
            "available": False,
            "reason": "Claude Code credentials not found — is Claude Code "
            "installed and logged in?",
        }
    try:
        creds = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "reason": f"Could not read credentials: {exc}"}

    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        return {"available": False, "reason": "No Claude Code login token found."}
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at / 1000 < time.time():
        return {
            "available": False,
            "reason": "Claude Code login token expired — open Claude Code "
            "once to refresh it.",
        }

    request = urllib.request.Request(
        _USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": _OAUTH_BETA,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return {
                "available": False,
                "reason": "Login token rejected — open Claude Code once to "
                "refresh it.",
            }
        return {
            "available": False,
            "reason": f"Usage endpoint returned HTTP {exc.code}.",
        }
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    windows = _parse_windows(payload)
    if not windows:
        return {
            "available": False,
            "reason": "Usage endpoint responded but no usage windows were "
            "recognized (endpoint format may have changed).",
        }
    windows.sort(key=lambda w: w["id"])
    return {
        "available": True,
        "plan": oauth.get("subscriptionType"),
        "windows": windows,
    }


def _codex_window_label(window_minutes) -> str:
    if window_minutes == 300:
        return "5-hour session"
    if window_minutes == 10080:
        return "Week (all usage)"
    if isinstance(window_minutes, (int, float)) and window_minutes > 0:
        hours = window_minutes / 60
        return f"{hours:.0f}-hour window"
    return "usage window"


def parse_codex_rate_limits(rate_limits: dict, now_ts: float | None = None) -> list[dict]:
    """Turn a codex `rate_limits` snapshot into the shared window shape.

    Snapshots come from session logs, so they age: if a window's reset time
    has already passed, that window rolled over since the snapshot — the
    recorded used_percent no longer applies and the window is fresh (0%).
    Without this, "100% used as of last night" keeps showing all morning.
    """
    now = time.time() if now_ts is None else now_ts
    windows: list[dict] = []
    for key in ("primary", "secondary"):
        bucket = rate_limits.get(key)
        if not isinstance(bucket, dict):
            continue
        used = bucket.get("used_percent")
        if not isinstance(used, (int, float)):
            continue
        resets_at = bucket.get("resets_at")
        already_reset = isinstance(resets_at, (int, float)) and resets_at < now
        windows.append(
            {
                "id": f"codex_{key}",
                "label": _codex_window_label(bucket.get("window_minutes")),
                "used_pct": 0.0 if already_reset else max(0.0, min(100.0, float(used))),
                "resets_at": None if already_reset else resets_at,
                "note": "window reset since last Codex run" if already_reset else None,
            }
        )
    return windows


def _codex_usage() -> dict:
    """Codex limits from its LOCAL session logs — the CLI has no headless
    usage command, but every session records `rate_limits` snapshots (the
    data its /status screen shows). Zero-cost read; numbers are as of the
    last Codex activity, so the panel labels them with that timestamp.
    """
    from classification_providers import find_codex

    if find_codex() is None:
        return {"available": False, "reason": "Codex CLI not installed."}

    sessions = Path.home() / ".codex" / "sessions"
    if not sessions.is_dir():
        return {
            "available": False,
            "reason": "No Codex sessions recorded yet — limits appear after "
            "the first Codex run.",
        }
    files = sorted(
        sessions.rglob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in files[:5]:
        try:
            # Newest snapshot wins; scan the file bottom-up. Session logs can
            # be a few MB — reading whole lines locally is fine.
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get("payload") or {}
            rate_limits = payload.get("rate_limits")
            if not isinstance(rate_limits, dict):
                continue
            windows = parse_codex_rate_limits(rate_limits)
            if not windows:
                continue
            return {
                "available": True,
                "plan": rate_limits.get("plan_type"),
                "windows": windows,
                "as_of": event.get("timestamp"),
            }
    return {
        "available": False,
        "reason": "No rate-limit snapshot found in recent Codex sessions.",
    }


def provider_usage() -> dict:
    """Cached usage snapshot for both subscription providers."""
    now = time.monotonic()
    if _cache["data"] is not None and now - _cache["at"] < _CACHE_TTL_SECONDS:
        return _cache["data"]
    data = {
        "claude": _claude_usage(),
        "codex": _codex_usage(),
        "fetched_at": time.time(),
    }
    _cache["at"] = now
    _cache["data"] = data
    return data
