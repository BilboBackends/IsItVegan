"""Keep a good stored menu when a re-scrape comes back worse.

A re-scrape replaces the stored text, and classification then deletes every
dish the new text doesn't mention. So one bad capture (a homepage, another
location, the first tab of a JS menu) silently empties a restaurant. In the
November 2026 re-scrape of 795 menus, about 1 in 5 came back that way.

The check asks whether the dishes we already list are still on the new page.
Comparing lines or characters doesn't work: the same menu re-rendered with
different markup looks almost entirely different that way.

A held menu isn't necessarily a bad capture, since restaurants do replace their
menus. Re-run with accept_changes to take the new one.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

HELD_PREFIX = "Kept the stored menu"

# Fewer recognizable dishes than this and the share that survived means little.
_MIN_KNOWN_DISHES = 5
# A stored menu this short was a partial capture; something over twice as long
# is an improvement even though its few dishes may be named differently.
_PARTIAL_MENU_CHARS = 1_500

_MOSTLY_DIFFERENT = 0.40  # share of known dishes kept, below which we hold
_LOST_DISHES = 0.80       # ... and below which a thinner page is suspicious
_MUCH_SHORTER = 0.60      # new length / old length
_WORDING_KEPT = 0.60      # share of the old wording still on a shorter page
_LITTLE_NEW = 0.15        # share of the new wording that wasn't there before


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _normalize(text: str) -> str:
    return re.sub(r"[\W_]+", " ", _fold(text)).strip()


def _wording(text: str) -> set[tuple[str, ...]]:
    """Three-word runs, so layout, prices and repeats don't count."""
    words = re.findall(r"[^\W\d_]{2,}", _fold(text))
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def hold_reason(
    old_text: str, new_text: str, dish_names: Iterable[str]
) -> str | None:
    """Why new_text shouldn't replace old_text, or None when it may."""
    old = f" {_normalize(old_text)} "
    new = f" {_normalize(new_text)} "
    # Only names the old page really showed: the classifier sometimes rewords
    # a dish, and those would read as missing from every page.
    names = {_normalize(name) for name in dish_names}
    known = sorted(name for name in names if name and f" {name} " in old)
    if len(known) < _MIN_KNOWN_DISHES:
        return None
    if len(old_text) < _PARTIAL_MENU_CHARS and len(new_text) > 2 * len(old_text):
        return None

    missing = [name for name in known if f" {name} " not in new]
    kept = 1 - len(missing) / len(known)
    if kept < _MOSTLY_DIFFERENT:
        problem = "the new page is mostly different"
    else:
        before, after = _wording(old_text), _wording(new_text)
        wording_kept = len(before & after) / max(1, len(before))
        new_wording = len(after - before) / max(1, len(after))
        much_shorter = len(new_text) < _MUCH_SHORTER * len(old_text)
        if much_shorter and (kept < _LOST_DISHES or wording_kept < _WORDING_KEPT):
            problem = "the new page is much shorter"
        elif kept < _LOST_DISHES and new_wording < _LITTLE_NEW:
            problem = "the new page lost dishes and adds nothing new"
        else:
            return None

    reason = (
        f"{HELD_PREFIX}: {problem}. It has {len(known) - len(missing)} of "
        f"{len(known)} known dishes ({len(old_text):,} -> {len(new_text):,} characters)."
    )
    if missing:
        reason += f" Missing, for example: {', '.join(missing[:4])}."
    return reason
