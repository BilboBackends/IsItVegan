"""The Vegan Score: 0-10, explainable, deterministic.

selection (0-5, log curve on strict vegan meals + quarter-weight sides) +
substance (0-3, how filling those meals are) + reputation (0-2, Google
rating scaled 3.0->5.0, neutral 1.0 when unrated).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vegan_score import compute_vegan_score  # noqa: E402


def test_zero_vegan_options_scores_only_reputation():
    score = compute_vegan_score(0, google_rating=4.6)
    assert score["selection"] == 0.0
    assert score["substance"] == 0.0
    assert score["reputation"] == 1.6
    assert score["score"] == 1.6


def test_selection_has_diminishing_returns():
    one = compute_vegan_score(1)["selection"]
    four = compute_vegan_score(4)["selection"]
    twelve = compute_vegan_score(12)["selection"]
    twenty = compute_vegan_score(20)["selection"]
    assert 0 < one < four < twelve
    assert twelve == 5.0  # saturation
    assert twenty == 5.0
    # The first few options matter more than the later ones.
    assert (four - one) > (twelve - four) / 2


def test_substance_rewards_filling_meals_not_salad_lists():
    salads = compute_vegan_score(6, high_protein_meals=0)
    tofu_bowls = compute_vegan_score(6, high_protein_meals=6)
    mixed = compute_vegan_score(6, high_protein_meals=2,
                                moderate_protein_meals=2)
    assert salads["substance"] == 0.0
    assert tofu_bowls["substance"] == 3.0
    assert 0 < mixed["substance"] < 3.0
    assert tofu_bowls["score"] > salads["score"]


def test_reputation_scales_and_clamps():
    assert compute_vegan_score(3, google_rating=5.0)["reputation"] == 2.0
    assert compute_vegan_score(3, google_rating=4.0)["reputation"] == 1.0
    assert compute_vegan_score(3, google_rating=2.1)["reputation"] == 0.0
    assert compute_vegan_score(3, google_rating=None)["reputation"] == 1.0


def test_sides_help_a_little():
    none = compute_vegan_score(2, vegan_sides=0)["selection"]
    some = compute_vegan_score(2, vegan_sides=4)["selection"]
    assert some > none
    # ...but four sides are worth less than one more meal.
    assert some < compute_vegan_score(3, vegan_sides=0)["selection"] + 0.01


def test_score_is_bounded_zero_to_ten():
    best = compute_vegan_score(
        50, vegan_sides=20, high_protein_meals=50, google_rating=5.0
    )
    assert best["score"] == 10.0
    worst = compute_vegan_score(0, google_rating=1.0)
    assert worst["score"] == 0.0
