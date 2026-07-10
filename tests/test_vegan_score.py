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
    # points: high protein 1.0/dish, vegan-named mains 0.9, moderate 0.6,
    # plain dishes 0.1 — saturating at 3, ABSOLUTE so breadth isn't punished.
    salads = compute_vegan_score(6, substance_points=6 * 0.1)
    tofu_bowls = compute_vegan_score(6, substance_points=6 * 1.0)
    vegan_pizza_line = compute_vegan_score(6, substance_points=6 * 0.9)
    one_bowl = compute_vegan_score(6, substance_points=1.0)
    assert salads["substance"] < 1.0
    assert tofu_bowls["substance"] == 3.0
    # A purpose-built vegan menu (Black Magic) is as substantial as tofu.
    assert vegan_pizza_line["substance"] == 3.0
    assert 0 < one_bowl["substance"] < 3.0
    assert tofu_bowls["score"] > salads["score"]


def test_menu_wide_plant_protein_earns_a_filling_point():
    # "add tofu +$3" lives in the menu, not the dish descriptions.
    without = compute_vegan_score(5, substance_points=0.5)
    with_addon = compute_vegan_score(
        5, substance_points=0.5, plant_protein_menu=True
    )
    assert with_addon["substance"] - without["substance"] == 1.0


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


def test_dessert_venues_score_substance_on_treat_variety():
    # An ice cream shop's treats are low-protein BY DESIGN — substance there
    # measures vegan variety, so Sampaguita's 7 flavors aren't a failed
    # dinner menu.
    protein_view = compute_vegan_score(7, google_rating=4.8)
    treat_view = compute_vegan_score(7, google_rating=4.8, dessert_venue=True)
    assert protein_view["substance"] == 0.0
    assert treat_view["substance"] == 3.0
    assert treat_view["score"] > protein_view["score"]
    assert treat_view["basis"] == "treat_variety"
    # One lone vegan flavor is still thin variety.
    assert compute_vegan_score(1, dessert_venue=True)["substance"] < 1.0


def test_score_is_bounded_zero_to_ten():
    best = compute_vegan_score(
        50, vegan_sides=20, substance_points=50, google_rating=5.0
    )
    assert best["score"] == 10.0
    worst = compute_vegan_score(0, google_rating=1.0)
    assert worst["score"] == 0.0
