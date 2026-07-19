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


_PRICE_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def canonical_price(value: str | None) -> str:
    """Numerically normalized price key: "$2.50", "$2.5", "2.50" agree.

    Alnum-only canonicalization treated $2.50 and $2.5 as DIFFERENT prices
    ("250" vs "25"), so a menu recapture that dropped trailing zeros
    duplicated every dish (Domu: VEGAN $15.50 next to Vegan $15.5). Multi
    prices keep their sequence ("$8 / $22" -> "8/22"); non-numeric prices
    ("market price") fall back to text canonicalization.
    """
    if not value:
        return ""
    numbers = _PRICE_NUMBER_RE.findall(str(value))
    if not numbers:
        return _canonical_text(value)
    return "/".join(format(float(number), "g") for number in numbers)


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
        canonical_price(price),
        _canonical_text(description),
        _canonical_text(calories),
    )


def dishes_compatible(
    key_a: tuple[str, str, str, str], key_b: tuple[str, str, str, str]
) -> bool:
    """Same dish even when one capture lacks detail the other has.

    Name and price must agree; a missing description or calories on ONE
    side is compatible with any value on the other (a recapture without
    descriptions must not duplicate the whole menu), while two CONFLICTING
    descriptions stay distinct dishes (same-name size/preparation variants).
    """
    if key_a[0] != key_b[0] or key_a[1] != key_b[1]:
        return False
    for a, b in zip(key_a[2:], key_b[2:]):
        if a and b and a != b:
            return False
    return True


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
