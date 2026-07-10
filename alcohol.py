"""Deterministic alcoholic / non-alcoholic drink labeling.

The model classifies alcohol_status alongside the vegan verdict, but drink
names are so formulaic that a word list settles most cases for free — used
as a validation backstop for every provider AND as the zero-cost backfill
for drinks classified before the attribute existed.

Precedence matters: explicit zero-proof markers ("virgin mojito",
"N/A cocktail") beat the alcohol word they decorate; then unambiguous
alcohol words; then soft-drink words; else unclear.
"""
from __future__ import annotations

import re

# Explicit "no alcohol in this" markers — checked FIRST so "virgin
# margarita" and "zero-proof negroni" land correctly.
_ZERO_PROOF_WORDS = (
    "virgin", "mocktail", "non-alcoholic", "non alcoholic", "zero proof",
    "zero-proof", "alcohol-free", "alcohol free", "0% abv",
)

_ALCOHOL_WORDS = (
    # beer / cider
    "beer", "ipa", "lager", "stout", "porter", "pilsner", "hefeweizen",
    "saison", "ale", "cider", "hard seltzer", "hard kombucha", "michelada",
    # wine / sparkling
    "wine", "chardonnay", "sauvignon", "cabernet", "merlot", "pinot",
    "riesling", "malbec", "prosecco", "champagne", "cava", "lambrusco",
    "sangria", "mimosa", "rosé", "port wine", "sherry", "vermouth",
    # spirits
    "whiskey", "whisky", "bourbon", "scotch", "rye", "tequila", "mezcal",
    "vodka", "gin", "rum", "brandy", "cognac", "absinthe", "schnapps",
    "soju", "sake", "shochu", "liqueur", "amaretto", "baileys", "kahlua",
    "aperol", "campari", "fireball", "jameson", "jack daniels",
    # cocktails
    "cocktail", "margarita", "martini", "mojito", "negroni",
    "old fashioned", "manhattan", "cosmopolitan", "daiquiri", "paloma",
    "spritz", "moscow mule", "mai tai", "pina colada", "piña colada",
    "bloody mary", "long island", "whiskey sour", "hot toddy", "sidecar",
    "boilermaker", "on the rocks", "single malt", "hard lemonade",
)

_SOFT_WORDS = (
    "soda", "coke", "coca-cola", "pepsi", "sprite", "fanta", "dr pepper",
    "root beer", "ginger ale", "ginger beer", "cola", "tonic water",
    "lemonade", "limeade", "juice", "orange juice",
    "iced tea", "sweet tea", "green tea", "herbal tea", "chai", "matcha",
    "coffee", "espresso", "latte", "cappuccino", "americano", "macchiato",
    "cold brew", "mocha", "hot chocolate", "smoothie", "milkshake",
    "milk shake", "shake", "float", "sparkling water",
    "mineral water", "bottled water", "still water", "kombucha",
    "agua fresca", "horchata", "boba", "bubble tea", "energy drink",
    "gatorade", "red bull", "milk", "oat milk", "almond milk", "cocoa",
    "frappe", "slush", "italian soda", "arnold palmer", "shirley temple",
)


def _word_re(words: tuple[str, ...]) -> re.Pattern:
    return re.compile(
        r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.I
    )


_ZERO_PROOF_RE = _word_re(_ZERO_PROOF_WORDS)
_ALCOHOL_RE = _word_re(_ALCOHOL_WORDS)
_SOFT_RE = _word_re(_SOFT_WORDS)

ALCOHOL_STATUSES = ("alcoholic", "non_alcoholic", "unclear")


def classify_alcohol(text: str) -> str:
    """alcoholic | non_alcoholic | unclear, from a drink's name/description."""
    if not text:
        return "unclear"
    if _ZERO_PROOF_RE.search(text):
        return "non_alcoholic"
    if _ALCOHOL_RE.search(text):
        return "alcoholic"
    if _SOFT_RE.search(text):
        return "non_alcoholic"
    return "unclear"
