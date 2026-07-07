"""Tests for the provider chain: ordering, failover, and limit cooldowns.

Transports are monkeypatched — no CLI, no network, no billing. What's pinned
here is the chain CONTRACT: auto prefers subscriptions (claude, codex) before
the metered API, any failure falls through to the next provider, and a
usage-limit failure puts a provider into cooldown so the rest of a bulk run
skips it instead of retrying a closed door per restaurant.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import classification_providers as cp  # noqa: E402
from classifier import result_from_data  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_module_state():
    cp._limited_until.clear()
    cp._availability_cache.clear()
    yield
    cp._limited_until.clear()
    cp._availability_cache.clear()


def _ok(provider):
    return cp.ProviderResponse(
        ok=True, provider=provider, model="m", billing=cp._BILLING[provider],
        data={"dishes": []},
    )


def _fail(provider, error):
    return cp.ProviderResponse(
        ok=False, provider=provider, model="m", billing=cp._BILLING[provider],
        error=error,
    )


def _all_available(monkeypatch):
    monkeypatch.setattr(cp, "_provider_available", lambda name: True)


def test_auto_chain_is_subscriptions_only(monkeypatch):
    # The metered APIs must NEVER be reachable from auto — metered billing
    # is opt-in, by explicit selection (which includes an explicitly
    # configured CLASSIFIER_PROVIDER chain in .env).
    assert cp._provider_chain("auto") == ["claude", "codex"]
    assert "anthropic" not in cp._AUTO_CHAIN
    assert "deepseek" not in cp._AUTO_CHAIN
    # With nothing requested, the CONFIGURED default chain applies — pin it
    # here so the test doesn't depend on the developer's .env.
    import dataclasses

    monkeypatch.setattr(
        cp, "settings", dataclasses.replace(cp.settings, classifier_provider="auto")
    )
    assert cp._provider_chain(None) == ["claude", "codex"]


def test_auto_never_falls_back_to_api(monkeypatch):
    # Both subscriptions exhausted -> the run FAILS rather than billing the
    # API behind the user's back.
    monkeypatch.setattr(cp, "_provider_available", lambda name: True)
    calls = []
    monkeypatch.setattr(
        cp, "_run_claude",
        lambda *a: calls.append("claude") or _fail("claude", "usage limit reached"),
    )
    monkeypatch.setattr(
        cp, "_run_codex",
        lambda *a: calls.append("codex") or _fail("codex", "rate limit exceeded"),
    )
    monkeypatch.setattr(
        cp, "_run_anthropic",
        lambda *a: calls.append("anthropic") or _ok("anthropic"),
    )
    response = cp.run_provider(
        requested="auto", system_prompt="s", user_prompt="u", schema={}
    )
    assert not response.ok
    assert "anthropic" not in calls


def test_custom_chain_and_validation():
    assert cp._provider_chain("codex,claude") == ["codex", "claude"]
    assert cp._provider_chain("anthropic") == ["anthropic"]
    with pytest.raises(cp.ProviderUnavailable):
        cp._provider_chain("gpt5")


def test_failover_on_any_error(monkeypatch):
    _all_available(monkeypatch)
    calls = []
    monkeypatch.setattr(
        cp, "_run_claude",
        lambda *a: calls.append("claude") or _fail("claude", "Malformed JSON"),
    )
    monkeypatch.setattr(
        cp, "_run_codex", lambda *a: calls.append("codex") or _ok("codex")
    )
    response = cp.run_provider(
        requested="auto", system_prompt="s", user_prompt="u", schema={}
    )
    assert response.ok and response.provider == "codex"
    assert calls == ["claude", "codex"]
    # An ordinary (non-limit) error must NOT put the provider in cooldown.
    assert not cp.provider_limited("claude")


def test_limit_error_sets_cooldown_and_is_skipped(monkeypatch):
    _all_available(monkeypatch)
    calls = []
    monkeypatch.setattr(
        cp, "_run_claude",
        lambda *a: calls.append("claude")
        or _fail("claude", "5-hour usage limit reached"),
    )
    monkeypatch.setattr(
        cp, "_run_codex", lambda *a: calls.append("codex") or _ok("codex")
    )
    first = cp.run_provider(
        requested="auto", system_prompt="s", user_prompt="u", schema={}
    )
    assert first.provider == "codex"
    assert cp.provider_limited("claude")

    # Next call in the same bulk run: claude is skipped without being tried.
    calls.clear()
    second = cp.run_provider(
        requested="auto", system_prompt="s", user_prompt="u", schema={}
    )
    assert second.provider == "codex"
    assert calls == ["codex"]


def test_cooldown_expires(monkeypatch):
    real_monotonic = time.monotonic
    cp._mark_limited("claude")
    assert cp.provider_limited("claude")
    monkeypatch.setattr(
        cp.time, "monotonic",
        lambda: real_monotonic() + cp._LIMIT_COOLDOWN_SECONDS + 1,
    )
    assert not cp.provider_limited("claude")


def test_all_limited_still_tries_rather_than_failing(monkeypatch):
    # If every provider in the chain is cooling down, retry them anyway —
    # a stale cooldown must not make classification impossible.
    _all_available(monkeypatch)
    cp._mark_limited("claude")
    monkeypatch.setattr(cp, "_run_claude", lambda *a: _ok("claude"))
    response = cp.run_provider(
        requested="claude", system_prompt="s", user_prompt="u", schema={}
    )
    assert response.ok and response.provider == "claude"


def test_resolve_provider_reports_chain_when_nothing_available(monkeypatch):
    monkeypatch.setattr(cp, "_provider_available", lambda name: False)
    with pytest.raises(cp.ProviderUnavailable, match="claude, codex"):
        cp.resolve_provider("auto")


def test_codex_rate_limit_snapshot_parses_to_windows():
    # Shape captured from a real ~/.codex session log (token_count event).
    from usage_limits import parse_codex_rate_limits

    snapshot = {
        "limit_id": "codex",
        "primary": {
            "used_percent": 100.0,
            "window_minutes": 300,
            "resets_at": 1783140990,
        },
        "secondary": {
            "used_percent": 34.0,
            "window_minutes": 10080,
            "resets_at": 1783709628,
        },
        "plan_type": "plus",
    }

    # Snapshot still current: report it verbatim.
    windows = parse_codex_rate_limits(snapshot, now_ts=1783140000)
    assert [w["label"] for w in windows] == ["5-hour session", "Week (all usage)"]
    assert windows[0]["used_pct"] == 100.0
    assert windows[0]["resets_at"] == 1783140990
    assert windows[1]["used_pct"] == 34.0

    # 5-hour window's reset time has passed: last night's 100% no longer
    # applies — the window is fresh. The weekly window is still live.
    windows = parse_codex_rate_limits(snapshot, now_ts=1783150000)
    assert windows[0]["used_pct"] == 0.0
    assert windows[0]["resets_at"] is None
    assert "reset" in windows[0]["note"]
    assert windows[1]["used_pct"] == 34.0
    assert windows[1]["note"] is None


def test_result_from_data_guards_non_list_attributes():
    # A hand-edited exchange file with string attributes must not be sliced
    # into one-character "ingredients".
    data = {
        "dishes": [
            {
                "name": "Tofu Bowl",
                "verdict": "vegan",
                "confidence": 0.9,
                "category": "food",
                "key_ingredients": "tofu, rice",
                "meal_types": "lunch",
            }
        ]
    }
    result = result_from_data(data, provider="claude", model="m", billing="x")
    assert result.ok
    assert result.dishes[0].key_ingredients == []
    assert result.dishes[0].meal_types == []
