"""Central configuration, loaded from environment / .env.

Every pipeline stage imports settings from here so nothing reads os.environ
directly. Keeps secrets out of code and makes the mock-first / testable
approach easier (override via env, no hardcoded keys).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load .env if present. Real values live in .env (gitignored); .env.example
# documents the shape. Missing .env is fine when running against mocks.
load_dotenv()


@dataclass(frozen=True)
class Settings:
    google_places_api_key: str | None
    anthropic_api_key: str | None
    classifier_provider: str
    anthropic_classifier_model: str
    claude_classifier_model: str | None
    claude_classifier_timeout_seconds: int
    codex_classifier_model: str | None
    codex_classifier_timeout_seconds: int
    discovery_lat: float
    discovery_lng: float
    discovery_radius_meters: float
    discovery_cell_radius_meters: float
    discovery_city: str | None
    database_path: str


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Env var {name}={raw!r} is not a valid number") from exc


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"Env var {name}={raw!r} is not a valid integer") from exc


def load_settings() -> Settings:
    return Settings(
        google_places_api_key=os.environ.get("GOOGLE_PLACES_API_KEY"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        classifier_provider=os.environ.get("CLASSIFIER_PROVIDER", "auto").lower(),
        anthropic_classifier_model=os.environ.get(
            "ANTHROPIC_CLASSIFIER_MODEL",
            os.environ.get("CLASSIFIER_MODEL", "claude-sonnet-5"),
        ),
        # Pin the CLI model explicitly: subscription defaults can be Opus
        # (bigger and slower than this extraction needs) and internal helper
        # calls muddy "what model ran". Sonnet is the smallest model that
        # holds up on the nuanced vegan-adaptable judgments.
        claude_classifier_model=(
            os.environ.get("CLAUDE_CLASSIFIER_MODEL") or "sonnet"
        ),
        claude_classifier_timeout_seconds=_get_int(
            "CLAUDE_CLASSIFIER_TIMEOUT_SECONDS", 900
        ),
        codex_classifier_model=(os.environ.get("CODEX_CLASSIFIER_MODEL") or None),
        codex_classifier_timeout_seconds=_get_int(
            "CODEX_CLASSIFIER_TIMEOUT_SECONDS", 900
        ),
        # Maitland, FL center as the MVP default.
        discovery_lat=_get_float("DISCOVERY_LAT", 28.6278),
        discovery_lng=_get_float("DISCOVERY_LNG", -81.3631),
        discovery_radius_meters=_get_float("DISCOVERY_RADIUS_METERS", 4000.0),
        # Grid cell radius. Smaller = more thorough but more API calls.
        # 1500m over the 4km Maitland area ≈ 49 calls/run.
        discovery_cell_radius_meters=_get_float(
            "DISCOVERY_CELL_RADIUS_METERS", 1500.0
        ),
        # Keep only results whose address is in this city. The area search
        # overshoots small towns into neighbors (Orlando, Winter Park). Set
        # empty to keep everything. MVP: Maitland only.
        discovery_city=(os.environ.get("DISCOVERY_CITY", "Maitland") or None),
        database_path=os.environ.get("DATABASE_PATH", "veganfind.db"),
    )


settings = load_settings()
