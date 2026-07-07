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
