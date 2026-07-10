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
import re

# Selection saturates here: this many effective meals earns the full 5.
_SELECTION_SATURATION = 12
_SIDE_WEIGHT = 0.25

# Substance saturates at this many "filling points" — roughly three
# protein-rich dishes, or three-plus purpose-built vegan mains.
_SUBSTANCE_SATURATION = 3.0

# Menu-wide vegan-protein signal: a kitchen that stocks tofu/seitan/vegan
# sausage can usually add it to dishes on request even when individual dish
# descriptions don't say so — worth one filling point on its own.
PLANT_PROTEIN_RE = re.compile(
    r"\b(tofu|seitan|tempeh|edamame|beyond|impossible"
    r"|vegan (?:protein|chick'?n|chicken|sausage|pepperoni|beef|steak|meat))\b",
    re.IGNORECASE,
)


def menu_offers_plant_protein(menu_text: str | None) -> bool:
    return bool(menu_text) and bool(PLANT_PROTEIN_RE.search(menu_text))


# A dessert venue with this many fully-vegan treats has a genuinely deep
# vegan lineup — earns the full substance credit.
_TREAT_VARIETY_SATURATION = 5


def compute_vegan_score(
    vegan_meals: int,
    vegan_sides: int = 0,
    substance_points: float = 0.0,
    google_rating: float | None = None,
    dessert_venue: bool = False,
    plant_protein_menu: bool = False,
) -> dict:
    """Score a restaurant. Returns {score, selection, substance,
    reputation, basis}.

    substance_points is the weighted "how filling" sum over counted vegan
    meals (db.verdict_counts_by_restaurant): high protein 1.0, purpose-built
    vegan-named dishes 0.9, moderate protein 0.6, everything else 0.1 —
    ABSOLUTE, not a fraction, so breadth is never punished: the question is
    "are there a few genuinely filling vegan options", not "what share".

    plant_protein_menu adds one point when the menu mentions tofu/seitan/
    vegan sausage anywhere — kitchens that stock vegan protein usually add
    it on request even when dish descriptions don't say so.

    dessert_venue switches the substance question entirely: at an ice cream
    shop nobody goes for protein — substance measures vegan TREAT VARIETY.
    """
    effective = max(0.0, vegan_meals + _SIDE_WEIGHT * vegan_sides)
    selection = 5.0 * min(
        1.0, math.log1p(effective) / math.log1p(_SELECTION_SATURATION)
    )

    if vegan_meals <= 0:
        substance = 0.0
    elif dessert_venue:
        substance = 3.0 * min(1.0, vegan_meals / _TREAT_VARIETY_SATURATION)
    else:
        points = max(0.0, substance_points)
        if plant_protein_menu:
            points += 1.0
        substance = 3.0 * min(1.0, points / _SUBSTANCE_SATURATION)

    if google_rating is None:
        reputation = 1.0  # unknown, not bad
    else:
        reputation = 2.0 * min(1.0, max(0.0, (float(google_rating) - 3.0) / 2.0))

    return {
        "score": round(selection + substance + reputation, 1),
        "selection": round(selection, 1),
        "substance": round(substance, 1),
        "reputation": round(reputation, 1),
        # what the substance number means — the tooltip says it honestly.
        "basis": "treat_variety" if dessert_venue else "protein",
    }
