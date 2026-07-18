"""Tests for the rule-based menu-likeness score."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from menu_score import score_menu_text  # noqa: E402

def test_dash_and_dot_leader_prices_count_as_prices():
    # The Wellborn's cocktail menu ("espresso martini - 15") scored 0.40
    # because the $-anchored regex saw zero prices in a real menu.
    menu = "\n".join(
        [
            "Seasonal Cocktails",
            "BEST FRIENDS",
            "purple gin, vanilla, lavender, lemon - 15",
            "TWO TICKETS",
            "vodka, strawberry fennel, watermelon, lemon - 13",
            "GUAVACITA",
            "white rum, guava, banana, lime, coconut cream - 15",
            "Garlic Buns . . . 9",
            "Zucchini Hummus . . . 10",
            "Kale Salad . . . 15",
            "Cabbage Pancakes . . . 8",
            "Whipped Feta . . . 12",
        ]
    )
    assert score_menu_text(menu).price_count >= 8

    # Years, addresses, and suite numbers never count.
    prose = "\n".join(
        [
            "Established in 1994",
            "145 S Orange Ave, Suite 201",
            "Open since 2019",
            "Call us at 407-555-0199",
        ]
    )
    assert score_menu_text(prose).price_count == 0
