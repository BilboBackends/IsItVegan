"""Background classification API behavior, including single-row reconnects."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api  # noqa: E402


class FakeThread:
    created: list["FakeThread"] = []

    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False
        self.__class__.created.append(self)

    def start(self):
        self.started = True


def _reset_state() -> None:
    api._classify_state.update(
        running=False,
        cancel_requested=False,
        total=None,
        done=0,
        succeeded=0,
        failed=0,
        cost=0.0,
        current=None,
        recent=[],
        summary=None,
        error=None,
        provider=None,
        billing=None,
    )
    api._classify_cancel.clear()
    FakeThread.created.clear()


def test_single_restaurant_reclassification_starts_reconnectable_job(monkeypatch):
    _reset_state()
    monkeypatch.setattr(api, "resolve_provider", lambda requested: "codex")
    monkeypatch.setattr(api.threading, "Thread", FakeThread)
    client = api.app.test_client()

    response = client.post(
        "/api/classify", json={"restaurant_id": 42, "provider": "codex"}
    )

    assert response.status_code == 202
    assert response.get_json() == {"started": True, "provider": "codex"}
    assert len(FakeThread.created) == 1
    thread = FakeThread.created[0]
    assert thread.started is True
    # (do_all, restaurant_ids, provider, restaurant_id, parallel, mode)
    assert thread.args == (False, None, "codex", 42, 3, "auto")

    # A newly loaded Admin page sees the same running job through status.
    status = client.get("/api/classify/status").get_json()
    assert status["running"] is True
    assert status["provider"] == "codex"
    assert status["billing"] == "chatgpt_subscription"
    _reset_state()


def test_single_restaurant_reclassification_rejects_invalid_id(monkeypatch):
    _reset_state()
    monkeypatch.setattr(api, "resolve_provider", lambda requested: "codex")
    client = api.app.test_client()

    response = client.post(
        "/api/classify", json={"restaurant_id": "42", "provider": "codex"}
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "restaurant_id must be an integer."
    _reset_state()
