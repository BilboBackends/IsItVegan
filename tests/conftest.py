"""Shared test isolation."""
from __future__ import annotations

import atexit
import dataclasses
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The app's default database is the developer's real one. A test that forgets to
# redirect or stub a call must not be able to read or write it, so point the
# default somewhere empty before anything imports config.
_scratch = tempfile.mkdtemp(prefix="veganfind-tests-")
atexit.register(shutil.rmtree, _scratch, ignore_errors=True)
os.environ["DATABASE_PATH"] = os.path.join(_scratch, "default.db")

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
