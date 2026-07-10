"""Tests for the cheap-model trust loop: guardrails, audits, and learning.

The contract: an untrusted (cheap) model's output never reaches the database
with a vegan verdict on a dish that plainly names an animal ingredient, every
intervention leaves an audit row, and spot-check disagreements become
corrections the model sees in its next prompt.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import db  # noqa: E402
import learning  # noqa: E402
from classifier import (  # noqa: E402
    ClassificationResult,
    ClassifiedDish,
    result_from_data,
)
from guardrails import apply_guardrails  # noqa: E402


def _dish(name, verdict, description=None, confidence=0.9, category="food",
          reasoning="", ingredients=None):
    return ClassifiedDish(
        name=name, description=description, price="$10", category=category,
        verdict=verdict, confidence=confidence, reasoning=reasoning,
        evidence="", key_ingredients=ingredients or [],
    )


def _result(dishes):
    return ClassificationResult(ok=True, dishes=dishes, provider="deepseek")


def test_animal_word_vegan_verdict_is_downgraded_and_flagged():
    result = _result([
        _dish("Caesar Salad", "vegan", "romaine, parmesan, croutons"),
    ])
    flags = apply_guardrails(result)
    assert result.dishes[0].verdict == "unclear"
    assert result.dishes[0].confidence <= 0.3
    assert flags and flags[0]["rule"] == "animal_ingredient_vegan"
    assert flags[0]["status"] == "downgraded"


def test_mock_qualified_animal_word_passes():
    result = _result([
        _dish("Vegan Mac", "vegan", "cashew cheese sauce, elbow pasta"),
        _dish("Chick'n Sandwich", "vegan", "soy chicken, vegan mayo"),
    ])
    flags = apply_guardrails(result)
    assert not flags
    assert all(d.verdict == "vegan" for d in result.dishes)


def test_plain_vegan_dishes_untouched():
    result = _result([
        _dish("Falafel Wrap", "vegan", "chickpeas, tahini, lettuce"),
        _dish("Garden Salad", "likely_vegan", "mixed greens, vinaigrette"),
    ])
    assert apply_guardrails(result) == []
    assert result.dishes[0].verdict == "vegan"


def test_implausible_vegan_rate_flags_run_without_downgrading():
    dishes = [
        _dish(f"Dish {i}", "vegan", "vegetables", confidence=0.5 + i * 0.01)
        for i in range(12)
    ]
    result = _result(dishes)
    flags = apply_guardrails(result)
    assert any(f["rule"] == "implausible_vegan_rate" for f in flags)
    assert all(d.verdict == "vegan" for d in result.dishes)  # flag only


def _raw(name, verdict, description=None, confidence=0.6):
    return {
        "name": name, "description": description, "price": "$10",
        "calories": None, "category": "food", "verdict": verdict,
        "confidence": confidence, "reasoning": "hedged", "evidence": "",
        "dairy_status": "unclear", "gluten_status": "unclear",
        "nut_status": "unclear", "protein_level": "unclear",
        "serving_role": "meal", "meal_types": [], "key_ingredients": [],
    }


def _validate(*dishes):
    return result_from_data(
        {"dishes": list(dishes)}, provider="deepseek", model="m", billing="x"
    )


def test_vegan_in_name_upgrades_hedged_verdicts():
    result = _validate(
        _raw("Vegan Burger", "likely_vegan"),
        _raw("Vegan Pad Thai", "unclear"),
        _raw("Vegan Mac", "vegan_adaptable"),
    )
    assert [d.verdict for d in result.dishes] == ["vegan"] * 3
    assert all(d.confidence >= 0.85 for d in result.dishes)
    assert "declares it vegan" in result.dishes[0].reasoning


def test_vegan_name_upgrade_respects_contradictions():
    result = _validate(
        # Description plainly names an unqualified animal ingredient.
        _raw("Vegan Cobb", "unclear", description="bacon, blue cheese, egg"),
        # The model outright said not_vegan — it may have seen something.
        _raw("Vegan Burger", "not_vegan"),
        # "vegan" as a substring of another word must not trigger.
        _raw("Veganesca Pasta Special", "unclear"),
        # No "vegan" in the name: untouched.
        _raw("Garden Burger", "likely_vegan"),
    )
    verdicts = [d.verdict for d in result.dishes]
    assert verdicts == ["unclear", "not_vegan", "unclear", "likely_vegan"]


def test_vegan_name_with_mock_qualified_description_still_upgrades():
    result = _validate(
        _raw("Vegan Philly", "likely_vegan",
             description="seitan steak, cashew cheese whiz"),
    )
    assert result.dishes[0].verdict == "vegan"


def test_output_cap_overflow_retries_in_chunks(monkeypatch):
    import classifier
    from classification_providers import ProviderResponse

    calls = []

    def fake_run_provider(*, requested, system_prompt, user_prompt, schema):
        calls.append(user_prompt)
        if len(calls) == 1:  # whole menu: overflows the output cap
            return ProviderResponse(
                ok=False, provider="deepseek", model="deepseek-chat",
                billing="deepseek_api",
                error="Output hit DeepSeek's max_tokens — menu too large",
                stop_reason="length",
            )
        # Each chunk succeeds with one dish named after its call number.
        return ProviderResponse(
            ok=True, provider="deepseek", model="deepseek-chat",
            billing="deepseek_api",
            data={"dishes": [dict(_raw(f"Dish {len(calls)}", "vegan"))]},
            input_tokens=100, output_tokens=50, cost_estimate=0.001,
        )

    monkeypatch.setattr(classifier, "run_provider", fake_run_provider)
    monkeypatch.setattr(classifier, "_CHUNK_TARGET_CHARS", 40)

    menu = "\n".join(f"Line {i} with some menu text here" for i in range(6))
    result = classifier.classify_menu(
        menu, restaurant_name="Big Menu Cafe", provider="deepseek"
    )
    assert result.ok
    assert result.mode == "full"
    assert len(calls) > 2  # initial overflow + one call per chunk
    assert len(result.dishes) == len(calls) - 1
    assert result.cost_estimate == pytest.approx(0.001 * (len(calls) - 1))


def test_plain_batter_dishes_never_stay_likely_vegan():
    # Standard pancake/waffle batter contains buttermilk and eggs even when
    # the menu only lists toppings — hedged vegan verdicts become unclear.
    result = _validate(
        _raw("Blueberry Waffles", "likely_vegan",
             description="fresh blueberries, maple syrup"),
        _raw("Banana Pancakes", "vegan", description="banana, walnuts"),
        _raw("Chocolate Croissant", "likely_vegan"),
    )
    assert [d.verdict for d in result.dishes] == ["unclear"] * 3
    assert all(d.confidence <= 0.4 for d in result.dishes)
    assert "batter" in result.dishes[0].reasoning


def test_traditionally_vegan_batters_are_exempt():
    # Asian "pancakes"/"crepes" are not Western buttermilk batter: scallion
    # pancakes are flour-water-oil, bánh xèo is rice flour + coconut milk.
    result = _validate(
        _raw("Scallion Pancake", "likely_vegan",
             description="crispy flour pancake, scallions"),
        _raw("Banh Xeo Chay - Crispy Golden Crepe", "vegan",
             description="rice flour, turmeric, bean sprouts"),
    )
    assert [d.verdict for d in result.dishes] == ["likely_vegan", "vegan"]


def test_marked_vegan_batter_dishes_are_exempt():
    result = _validate(
        # Name declares vegan: the upgrade rule applies, batter rule stands
        # down (qualifier present).
        _raw("Vegan Waffles", "likely_vegan", description="oat milk batter"),
        # Description declares the plant version.
        _raw("Sunday Pancakes", "likely_vegan",
             description="plant-based buttermilk batter, aquafaba"),
        # not_vegan verdicts are never touched.
        _raw("Belgian Waffle", "not_vegan"),
    )
    assert [d.verdict for d in result.dishes] == [
        "vegan", "likely_vegan", "not_vegan",
    ]


def test_namesake_animal_ingredient_kills_vegan_adaptable():
    # A cheese empanada without cheese isn't an empanada — "adaptable" only
    # makes sense when the dish survives the removal.
    result = _validate(
        _raw("Cheese Empanada", "vegan_adaptable", confidence=0.7),
        _raw("Cheese Pizza", "vegan_adaptable",
             description="mozzarella, red sauce"),
        _raw("Shrimp Fried Rice", "vegan_adaptable"),
    )
    assert [d.verdict for d in result.dishes] == ["not_vegan"] * 3
    assert all(d.confidence >= 0.8 for d in result.dishes)
    assert "namesake" in result.dishes[0].reasoning


def test_removable_toppings_keep_vegan_adaptable():
    result = _validate(
        # Name is clean — the feta is a topping, the salad survives.
        _raw("Greek Salad", "vegan_adaptable",
             description="cucumber, olives, feta — hold the feta"),
        # Explicit vegan option: the name declares it, upgrade rule wins.
        _raw("Vegan Cheese Pizza", "vegan_adaptable",
             description="cashew mozzarella"),
    )
    assert result.dishes[0].verdict == "vegan_adaptable"
    assert result.dishes[1].verdict == "vegan"


def test_dense_chunks_split_adaptively_until_they_fit(monkeypatch):
    # A dense menu (170 dishes in one 12k chunk) can overflow the output cap
    # even after the first split — the section must then split again instead
    # of failing the whole menu (the Anh Hong bug).
    import classifier
    from classification_providers import ProviderResponse

    def overflow():
        return ProviderResponse(
            ok=False, provider="deepseek", model="deepseek-chat",
            billing="deepseek_api", error="Output hit DeepSeek's max_tokens",
            stop_reason="length",
        )

    calls = []

    def fake_run_provider(*, requested, system_prompt, user_prompt, schema):
        calls.append(user_prompt)
        menu_part = user_prompt.split(":\n\n", 1)[-1]
        # Anything holding more than one menu line still "overflows".
        if menu_part.count("Line ") > 1:
            return overflow()
        return ProviderResponse(
            ok=True, provider="deepseek", model="deepseek-chat",
            billing="deepseek_api",
            data={"dishes": [dict(_raw(f"Dish {len(calls)}", "vegan"))]},
            input_tokens=100, output_tokens=50, cost_estimate=0.001,
        )

    monkeypatch.setattr(classifier, "run_provider", fake_run_provider)
    monkeypatch.setattr(classifier, "_CHUNK_TARGET_CHARS", 80)
    monkeypatch.setattr(classifier, "_MIN_CHUNK_CHARS", 10)

    menu = "\n".join(f"Line {i} of a dense vietnamese menu" for i in range(4))
    result = classifier.classify_menu(
        menu, restaurant_name="Dense Menu Cafe", provider="deepseek"
    )
    assert result.ok
    assert len(result.dishes) == 4  # one per line, nothing lost
    # 1 full-menu overflow + 2 two-line overflows + 4 single-line successes.
    assert len(calls) == 7


@pytest.fixture()
def test_db(tmp_path):
    path = str(tmp_path / "audit.db")
    db.init_db(path)
    with db.connect(path) as conn:
        conn.execute(
            "INSERT INTO restaurants (id, name, place_id) VALUES (1, 'Cafe', 'p1')"
        )
    return path


def test_audits_recorded_and_summarized(test_db):
    db.record_audits(
        [
            {"check_type": "guardrail", "rule": "animal_ingredient_vegan",
             "dish_name": "Caesar", "status": "downgraded", "detail": "x"},
            {"check_type": "spot_check", "rule": "verdict_match",
             "dish_name": "Pad Thai", "status": "disagree",
             "expected_verdict": "not_vegan", "actual_verdict": "likely_vegan"},
            {"check_type": "spot_check", "rule": "verdict_match",
             "dish_name": "Falafel", "status": "agree"},
        ],
        provider="deepseek",
        model="deepseek-chat",
        restaurant_id=1,
        db_path=test_db,
    )
    summary = db.audit_summary(db_path=test_db)
    ds = summary["providers"]["deepseek"]
    assert ds["guardrail_downgraded"] == 1
    assert ds["spot_check_agree"] == 1
    assert ds["spot_check_disagree"] == 1
    assert ds["spot_check_agreement"] == 0.5
    audits = db.list_audits(db_path=test_db)
    assert len(audits) == 3
    assert audits[0]["restaurant_name"] == "Cafe"


def test_corrections_feed_the_guidance_block(test_db):
    db.record_correction(
        "Pad Thai", "likely_vegan", "not_vegan",
        note="fish sauce and egg are standard", db_path=test_db,
    )
    # A newer correction for the SAME dish replaces the old one.
    db.record_correction(
        "Pad Thai", "vegan", "not_vegan",
        note="fish sauce standard", db_path=test_db,
    )
    active = db.list_corrections(db_path=test_db)
    assert len(active) == 1
    assert active[0]["wrong_verdict"] == "vegan"

    block = learning.guidance_block(db_path=test_db)
    assert "LEARNED CORRECTIONS" in block
    assert "Pad Thai" in block and "not_vegan" in block


def test_guidance_block_empty_without_corrections(test_db):
    assert learning.guidance_block(db_path=test_db) is None
