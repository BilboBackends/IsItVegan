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
    discovery_lat: float
    discovery_lng: float
    discovery_radius_meters: float
    database_path: str


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Env var {name}={raw!r} is not a valid number") from exc


def load_settings() -> Settings:
    return Settings(
        google_places_api_key=os.environ.get("GOOGLE_PLACES_API_KEY"),
        # Maitland, FL center as the MVP default.
        discovery_lat=_get_float("DISCOVERY_LAT", 28.6278),
        discovery_lng=_get_float("DISCOVERY_LNG", -81.3631),
        discovery_radius_meters=_get_float("DISCOVERY_RADIUS_METERS", 4000.0),
        database_path=os.environ.get("DATABASE_PATH", "veganfind.db"),
    )


settings = load_settings()
