"""Shared test isolation."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import google_usage  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_google_usage(tmp_path, monkeypatch):
    """Mocked Google calls must never count in the developer's real database."""
    monkeypatch.setattr(
        google_usage,
        "settings",
        dataclasses.replace(
            google_usage.settings, database_path=str(tmp_path / "google_usage.db")
        ),
    )
    google_usage._pending.clear()
    yield
    google_usage._pending.clear()
