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
    photo_menu_vision_model: str
    google_vision_api_key: str | None
    claude_classifier_model: str | None
    claude_classifier_timeout_seconds: int
    codex_cli_path: str | None
    codex_classifier_model: str | None
    codex_classifier_timeout_seconds: int
    deepseek_api_key: str | None
    deepseek_classifier_model: str
    deepseek_base_url: str
    deepseek_classifier_timeout_seconds: int
    deepseek_guardrails: bool
    deepseek_max_output_tokens: int
    discovery_lat: float
    discovery_lng: float
    discovery_radius_meters: float
    discovery_cell_radius_meters: float
    discovery_city: str | None
    database_path: str
    supabase_url: str | None
    supabase_service_role_key: str | None


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
        classifier_provider=os.environ.get("CLASSIFIER_PROVIDER", "deepseek").lower(),
        anthropic_classifier_model=os.environ.get(
            "ANTHROPIC_CLASSIFIER_MODEL",
            os.environ.get("CLASSIFIER_MODEL", "claude-sonnet-5"),
        ),
        # Menu-image transcription (photo_menu.py) when OCR isn't enough.
        # Haiku is the cheap vision rung (same tier pdf_menu.py uses for
        # PDF transcription); photo_menu escalates to Opus on its own when
        # a Haiku read fails the menu gates.
        photo_menu_vision_model=os.environ.get(
            "PHOTO_MENU_VISION_MODEL", "claude-haiku-4-5"
        ),
        # Cheap OCR tier for menu images (~$1.50/1000 vs ~$0.05/image for
        # Claude vision). Falls back to the Places key — same Google Cloud
        # project, just needs the Vision API enabled on it.
        google_vision_api_key=(
            os.environ.get("GOOGLE_VISION_API_KEY")
            or os.environ.get("GOOGLE_PLACES_API_KEY")
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
        codex_cli_path=(os.environ.get("CODEX_CLI_PATH") or None),
        codex_classifier_model=(os.environ.get("CODEX_CLASSIFIER_MODEL") or None),
        codex_classifier_timeout_seconds=_get_int(
            "CODEX_CLASSIFIER_TIMEOUT_SECONDS", 900
        ),
        # DeepSeek is the sole default classifier. Other transports are kept
        # as explicit manual overrides and are never automatic fallbacks.
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY"),
        deepseek_classifier_model=os.environ.get(
            "DEEPSEEK_CLASSIFIER_MODEL", "deepseek-chat"
        ),
        deepseek_base_url=os.environ.get(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        ),
        deepseek_classifier_timeout_seconds=_get_int(
            "DEEPSEEK_CLASSIFIER_TIMEOUT_SECONDS", 900
        ),
        # Automatic DeepSeek auditing and verdict downgrades are off by default.
        deepseek_guardrails=os.environ.get(
            "DEEPSEEK_GUARDRAILS", "0"
        ).strip().lower() not in ("0", "false", "no", "off"),
        # DeepSeek caps output per response: deepseek-chat allows up to 8192;
        # deepseek-reasoner allows much more — raise this if you switch.
        deepseek_max_output_tokens=_get_int(
            "DEEPSEEK_MAX_OUTPUT_TOKENS", 8192
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
        # User-data plane (Supabase) for the Admin activity page. The service
        # role key bypasses RLS — server-side only, never sent to a frontend.
        supabase_url=(os.environ.get("SUPABASE_URL") or None),
        supabase_service_role_key=(
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or None
        ),
    )


settings = load_settings()
