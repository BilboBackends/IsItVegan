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


def _result(dishes, *, fully_vegan_venue=False):
    return classifier.result_from_data(
        {"dishes": dishes},
        provider="deepseek",
        model="deepseek-test",
        billing="deepseek_api",
        fully_vegan_venue=fully_vegan_venue,
    )


def _dairy_dish(name, verdict, **over):
    d = {
        "name": name,
        "description": over.pop("description", "House preparation"),
        "price": "$17.95",
        "category": "food",
        "verdict": verdict,
        "confidence": 0.75,
        "reasoning": "Model interpretation.",
        "evidence": name,
        "dairy_status": "contains",
    }
    d.update(over)
    return d


def test_dairy_contains_caps_plant_verdict_to_adaptable():
    # A veggie bowl the model marked likely_vegan while tagging
    # dairy_status=contains — the dairy is a side of sour cream named only in
    # the description (not the dish's namesake), so it becomes adaptable and no
    # longer reaches the strict headline vegan count.
    dish = _dairy_dish(
        "Southwest Veggie Bowl", "likely_vegan",
        description="Rice, black beans, corn, avocado, served with sour cream",
    )
    out = _result([dish]).dishes[0]
    assert out.verdict == "vegan_adaptable"
    assert out.dairy_status == "contains"


def test_dairy_contains_when_named_after_dairy_is_not_vegan():
    # Cheesecake Factory "Barbeque Ranch Chicken Salad": likely_vegan yet
    # dairy_status=contains. Both the ranch (dairy dressing) AND chicken are in
    # the name, so the dish is named after its animal components — not_vegan,
    # not a removable modification.
    dish = _dairy_dish("Barbeque Ranch Chicken Salad", "likely_vegan")
    out = _result([dish]).dishes[0]
    assert out.verdict == "not_vegan"


def test_dairy_contains_when_definitional_is_not_vegan():
    # "Cheese Quesadilla" marked vegan with dairy_status=contains: the cheese
    # is the dish's namesake, so removing it doesn't leave the dish — not_vegan.
    dish = _dairy_dish("Cheese Quesadilla", "vegan")
    out = _result([dish]).dishes[0]
    assert out.verdict == "not_vegan"


def test_dairy_conflict_guard_skips_verified_vegan_venue():
    # A verified vegan venue's plant "cheese"/"ranch" may carry a stray
    # dairy_status=contains tag; the verdict must stay vegan, never downgraded.
    dish = _dairy_dish("Ranch Dressing", "vegan")
    out = _result([dish], fully_vegan_venue=True).dishes[0]
    assert out.verdict == "vegan"


def test_dairy_free_plant_verdict_untouched():
    # The guard only fires on dairy_status=contains — a genuinely dairy-free
    # vegan dish is left exactly as the model returned it.
    dish = _dairy_dish("Garden Salad", "vegan", dairy_status="free")
    out = _result([dish]).dishes[0]
    assert out.verdict == "vegan"


def test_menu_declared_vegan_dish_survives_stray_dairy_tag():
    # "Vegan Brekkie Sandwich" with plant cheese the model mis-tagged
    # dairy_status=contains: the menu's own "vegan" label wins, verdict stays.
    dish = _dairy_dish(
        "Vegan Brekkie Sandwich", "vegan",
        description="Sourdough, avocado, plant cheese, tomato",
    )
    out = _result([dish]).dishes[0]
    assert out.verdict == "vegan"
