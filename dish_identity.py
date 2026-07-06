"""Conservative identity rules for duplicate menu dishes.

Scraped menus commonly repeat the same product in a featured section and its
normal category with different capitalization or punctuation. Names alone are
not enough to merge safely: two sections can use the same short name for
different sizes, sides, or prices. Identity therefore includes the normalized
name, price, description, and explicit calories.
"""
from __future__ import annotations

import re
import unicodedata


def _canonical_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(character for character in normalized if character.isalnum())


def canonical_dish_name(name: str) -> str:
    """Case/spacing/punctuation-insensitive key used only for comparison."""
    return _canonical_text(name)


def dish_identity_key(
    name: str,
    price: str | None,
    description: str | None,
    calories: str | None = None,
) -> tuple[str, str, str, str]:
    """Identity strict enough to preserve same-name menu variants."""
    return (
        canonical_dish_name(name),
        _canonical_text(price),
        _canonical_text(description),
        _canonical_text(calories),
    )


def _display_quality(name: str) -> tuple[int, int, int, int]:
    letters = "".join(character for character in name if character.isalpha())
    all_upper = bool(letters) and letters == letters.upper() and letters != letters.lower()
    title_like = bool(re.search(r"[A-Za-z]", name)) and name.istitle()
    awkward_symbol_space = bool(re.search(r"\s+[Ⓥⓥ]$", name))
    return (
        0 if all_upper else 1,
        1 if title_like else 0,
        0 if awkward_symbol_space else 1,
        -len(name),
    )


def preferred_dish_name(first: str, second: str) -> str:
    """Choose the more readable spelling without inventing a new name."""
    return second if _display_quality(second) > _display_quality(first) else first
