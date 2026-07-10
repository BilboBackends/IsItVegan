"""The Vegan Score: one 0-10 number for "how good is this restaurant for a
vegan", computed from data we already trust and always explainable:

- selection  (0-5): strict vegan MEALS with diminishing returns (a log
  curve: the jump from 0->3 options matters far more than 10->13); vegan
  sides count a quarter each.
- substance  (0-3): fraction of those meals that are actually filling —
  high-protein counts fully, moderate partially. A list of garden salads
  scores near zero here; tofu bowls score high.
- reputation (0-2): the restaurant's Google rating scaled from 3.0 (0) to
  5.0 (2). Unrated places get a neutral 1.0 rather than a penalty.

Deterministic and server-side: the score ships in /api/restaurants and the
static export, so the site never recomputes or drifts. Components ride
along for tooltips — never show a number without its why (CLAUDE.md).
"""
from __future__ import annotations

import math

# Selection saturates here: this many effective meals earns the full 5.
_SELECTION_SATURATION = 12
_SIDE_WEIGHT = 0.25


def compute_vegan_score(
    vegan_meals: int,
    vegan_sides: int = 0,
    high_protein_meals: int = 0,
    moderate_protein_meals: int = 0,
    google_rating: float | None = None,
) -> dict:
    """Score a restaurant. Returns {score, selection, substance, reputation}."""
    effective = max(0.0, vegan_meals + _SIDE_WEIGHT * vegan_sides)
    selection = 5.0 * min(
        1.0, math.log1p(effective) / math.log1p(_SELECTION_SATURATION)
    )

    if vegan_meals > 0:
        filling = min(
            1.0,
            (high_protein_meals + 0.6 * moderate_protein_meals) / vegan_meals,
        )
        substance = 3.0 * filling
    else:
        substance = 0.0

    if google_rating is None:
        reputation = 1.0  # unknown, not bad
    else:
        reputation = 2.0 * min(1.0, max(0.0, (float(google_rating) - 3.0) / 2.0))

    return {
        "score": round(selection + substance + reputation, 1),
        "selection": round(selection, 1),
        "substance": round(substance, 1),
        "reputation": round(reputation, 1),
    }
