"""Tests for DeepSeek-only provider selection and limit cooldowns.

Transports are monkeypatched — no CLI, no network, no billing. What's pinned
here is the contract: auto is DeepSeek only and no alternative provider can be
selected or used as fallback.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
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


def test_auto_chain_is_deepseek_only(monkeypatch):
    assert cp._provider_chain("auto") == ["deepseek"]
    assert cp._AUTO_CHAIN == ("deepseek",)
    assert cp.UNTRUSTED_PROVIDERS == frozenset()
    assert cp.CHUNKED_PROVIDERS == frozenset({"deepseek"})
    # With nothing requested, the CONFIGURED default chain applies — pin it
    # here so the test doesn't depend on the developer's .env.
    import dataclasses

    monkeypatch.setattr(
        cp, "settings", dataclasses.replace(cp.settings, classifier_provider="auto")
    )
    assert cp._provider_chain(None) == ["deepseek"]


# 2026-11-25 is a Wednesday: 12:00 UTC is off-peak, 02:00 UTC is peak.
_OFF_PEAK = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
_PEAK = datetime(2026, 11, 25, 2, tzinfo=timezone.utc)


def test_retired_deepseek_names_are_priced_as_flash():
    # V4-Flash is served and billed as deepseek-flash since its retirement.
    assert cp.PRICES["deepseek-flash"] == (0.15, 0.60)
    for alias in ("deepseek-v4-flash", "deepseek-reasoner", "deepseek-chat"):
        assert cp.PRICES[alias] == cp.PRICES["deepseek-flash"]


@pytest.mark.parametrize(
    "hour, minute, peak",
    [(0, 59, False), (1, 0, True), (3, 59, True), (4, 0, False),
     (5, 59, False), (6, 0, True), (9, 59, True), (10, 0, False)],
)
def test_deepseek_peak_hours_double_the_price(hour, minute, peak):
    at = datetime(2026, 11, 25, hour, minute, tzinfo=timezone.utc)
    expected = (0.30, 1.20) if peak else (0.15, 0.60)
    assert cp.price_for("deepseek-v4-flash", at=at) == pytest.approx(expected)


def test_deepseek_weekends_are_always_off_peak():
    saturday = datetime(2026, 11, 28, 2, tzinfo=timezone.utc)
    assert cp.price_for("deepseek-flash", at=saturday) == (0.15, 0.60)


def test_deepseek_peak_uses_utc_not_local_time():
    # Tuesday 9pm Eastern is Wednesday 02:00 UTC.
    eastern = timezone(timedelta(hours=-5))
    at = datetime(2026, 11, 24, 21, tzinfo=eastern)
    assert cp.price_for("deepseek-flash", at=at) == pytest.approx((0.30, 1.20))


def test_peak_pricing_is_deepseek_only():
    assert cp.price_for("claude-sonnet-5", at=_PEAK) == (3.0, 15.0)


def test_estimate_cost_prices_against_running_provider(monkeypatch):
    import classifier

    monkeypatch.setattr(cp, "_utcnow", lambda: _OFF_PEAK)
    # Default estimate is the cheap DeepSeek tier, not the Anthropic API model.
    deepseek_est = classifier.estimate_cost(20_000, provider="deepseek")
    anthropic_est = classifier.estimate_cost(20_000, provider="anthropic")
    assert 0.02 < deepseek_est < 0.05  # ~3 cents for a 20k-char menu
    # Anthropic (Sonnet) is far pricier — the estimate must reflect that so a
    # metered run is not undersold.
    assert anthropic_est > deepseek_est * 5


def test_estimate_cost_reproduces_recorded_july_spend(monkeypatch):
    # 530 full runs: 10,110,570 menu chars cost $8.40 at the rates then in
    # force. The old formula came out at about a third of that.
    import classifier

    monkeypatch.setattr(classifier, "price_for", lambda model: (0.14, 0.28))
    average_menu = 10_110_570 // 530
    estimate = classifier.estimate_cost(average_menu, provider="deepseek") * 530
    assert estimate == pytest.approx(8.40, rel=0.10)


def test_estimate_cost_doubles_at_peak(monkeypatch):
    import classifier

    monkeypatch.setattr(cp, "_utcnow", lambda: _OFF_PEAK)
    off_peak = classifier.estimate_cost(50_000, provider="deepseek")
    monkeypatch.setattr(cp, "_utcnow", lambda: _PEAK)
    peak = classifier.estimate_cost(50_000, provider="deepseek")
    assert peak == pytest.approx(off_peak * 2, abs=0.002)  # both rounded


@pytest.mark.parametrize(
    "now, peak, changes, back",
    [
        # Wednesday: off-peak until 01:00, peak 01-04, gap, peak 06-10.
        ((25, 12, 30), False, (26, 1), (26, 4)),
        ((26, 2, 0), True, (26, 4), (26, 6)),
        ((26, 4, 0), False, (26, 6), (26, 10)),
        ((26, 9, 59), True, (26, 10), (27, 1)),
        # Friday after 10:00 stays off-peak through the weekend.
        ((27, 10, 0), False, (30, 1), (30, 4)),
        ((29, 2, 0), False, (30, 1), (30, 4)),
    ],
)
def test_deepseek_rate_names_the_next_two_changes(now, peak, changes, back):
    rate = cp.deepseek_rate(datetime(2026, 11, *now, tzinfo=timezone.utc))
    assert rate["peak"] is peak
    assert rate["changes_at"] == datetime(
        2026, 11, *changes, tzinfo=timezone.utc
    ).isoformat()
    assert rate["changes_back_at"] == datetime(
        2026, 11, *back, tzinfo=timezone.utc
    ).isoformat()


def test_provider_status_carries_the_current_rate(monkeypatch):
    monkeypatch.setattr(cp, "_utcnow", lambda: _PEAK)
    rate = cp.provider_status()["providers"]["deepseek"]["rate"]
    assert rate["peak"] is True and rate["peak_multiplier"] == 2.0


def test_estimate_cost_defaults_to_configured_provider(monkeypatch):
    import dataclasses

    import classifier

    monkeypatch.setattr(cp, "_utcnow", lambda: _OFF_PEAK)
    monkeypatch.setattr(
        cp, "settings", dataclasses.replace(cp.settings, classifier_provider="deepseek")
    )
    monkeypatch.setattr(
        classifier, "settings",
        dataclasses.replace(classifier.settings, classifier_provider="deepseek"),
    )
    # No provider arg -> the configured default (DeepSeek), not Anthropic.
    assert classifier.estimate_cost(20_000) == classifier.estimate_cost(
        20_000, provider="deepseek"
    )


def test_model_for_provider_maps_deepseek_to_configured_model():
    assert cp.model_for_provider("deepseek") == cp.settings.deepseek_classifier_model
    assert cp.model_for_provider("anthropic") == cp.settings.anthropic_classifier_model


def test_auto_never_falls_back_from_deepseek(monkeypatch):
    monkeypatch.setattr(cp, "_provider_available", lambda name: True)
    calls = []
    monkeypatch.setattr(
        cp, "_run_deepseek",
        lambda *a: calls.append("deepseek") or _fail("deepseek", "rate limit reached"),
    )
    monkeypatch.setattr(
        cp, "_run_anthropic",
        lambda *a: calls.append("anthropic") or _ok("anthropic"),
    )
    response = cp.run_provider(
        requested="auto", system_prompt="s", user_prompt="u", schema={}
    )
    assert not response.ok
    assert calls == ["deepseek"]


@pytest.mark.parametrize("provider", ["claude", "codex", "anthropic", "claude,codex"])
def test_non_deepseek_provider_is_rejected(provider):
    with pytest.raises(cp.ProviderUnavailable, match="only enabled"):
        cp._provider_chain(provider)


def test_deepseek_failure_does_not_fall_back(monkeypatch):
    _all_available(monkeypatch)
    calls = []
    monkeypatch.setattr(
        cp, "_run_deepseek",
        lambda *a: calls.append("deepseek") or _fail("deepseek", "Malformed JSON"),
    )
    monkeypatch.setattr(
        cp, "_run_codex", lambda *a: calls.append("codex") or _ok("codex")
    )
    response = cp.run_provider(
        requested="auto", system_prompt="s", user_prompt="u", schema={}
    )
    assert not response.ok
    assert calls == ["deepseek"]
    # An ordinary (non-limit) error must NOT put the provider in cooldown.
    assert not cp.provider_limited("deepseek")


def test_limit_error_sets_cooldown_and_is_skipped(monkeypatch):
    _all_available(monkeypatch)
    calls = []
    monkeypatch.setattr(
        cp, "_run_deepseek",
        lambda *a: calls.append("deepseek")
        or _fail("deepseek", "rate limit reached"),
    )
    monkeypatch.setattr(
        cp, "_run_codex", lambda *a: calls.append("codex") or _ok("codex")
    )
    first = cp.run_provider(
        requested="auto", system_prompt="s", user_prompt="u", schema={}
    )
    assert first.provider == "deepseek"
    assert cp.provider_limited("deepseek")

    # Next call in the same bulk run: claude is skipped without being tried.
    calls.clear()
    second = cp.run_provider(
        requested="auto", system_prompt="s", user_prompt="u", schema={}
    )
    assert second.provider == "deepseek"
    assert calls == ["deepseek"]


def test_cooldown_expires(monkeypatch):
    real_monotonic = time.monotonic
    cp._mark_limited("deepseek")
    assert cp.provider_limited("deepseek")
    monkeypatch.setattr(
        cp.time, "monotonic",
        lambda: real_monotonic() + cp._LIMIT_COOLDOWN_SECONDS + 1,
    )
    assert not cp.provider_limited("deepseek")


def test_all_limited_still_tries_rather_than_failing(monkeypatch):
    # If every provider in the chain is cooling down, retry them anyway —
    # a stale cooldown must not make classification impossible.
    _all_available(monkeypatch)
    cp._mark_limited("deepseek")
    monkeypatch.setattr(cp, "_run_deepseek", lambda *a: _ok("deepseek"))
    response = cp.run_provider(
        requested="deepseek", system_prompt="s", user_prompt="u", schema={}
    )
    assert response.ok and response.provider == "deepseek"


def test_resolve_provider_reports_chain_when_nothing_available(monkeypatch):
    monkeypatch.setattr(cp, "_provider_available", lambda name: False)
    with pytest.raises(cp.ProviderUnavailable, match="deepseek"):
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
