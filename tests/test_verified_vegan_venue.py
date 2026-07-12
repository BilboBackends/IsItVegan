"""Exact fully-vegan venues must not be downgraded by recipe backstops."""
from __future__ import annotations

import classifier
from classification_providers import ProviderResponse


def _dish(name: str, verdict: str) -> dict:
    return {
        "name": name,
        "description": "House preparation",
        "price": "$12",
        "category": "food",
        "verdict": verdict,
        "confidence": 0.8,
        "reasoning": "Provider interpretation.",
        "evidence": name,
    }


def test_verified_vegan_venue_overrides_animal_style_names(monkeypatch):
    monkeypatch.setattr(
        classifier,
        "run_provider",
        lambda **kwargs: ProviderResponse(
            ok=True,
            provider="deepseek",
            model="deepseek-test",
            billing="deepseek_api",
            data={"dishes": [_dish("Buffalo Chik Sub", "not_vegan")]},
        ),
    )

    result = classifier.classify_menu(
        "Buffalo Chik Sub", restaurant_name="Winter Park Biscuit Company"
    )

    assert result.ok
    assert result.dishes[0].verdict == "vegan"
    assert result.dishes[0].confidence == 0.99


def test_ordinary_restaurant_gets_no_venue_override(monkeypatch):
    monkeypatch.setattr(
        classifier,
        "run_provider",
        lambda **kwargs: ProviderResponse(
            ok=True,
            provider="deepseek",
            model="deepseek-test",
            billing="deepseek_api",
            data={"dishes": [_dish("Chicken Pita", "not_vegan")]},
        ),
    )

    result = classifier.classify_menu(
        "Chicken Pita", restaurant_name="Ordinary Restaurant"
    )

    assert result.ok
    assert result.dishes[0].verdict == "not_vegan"
