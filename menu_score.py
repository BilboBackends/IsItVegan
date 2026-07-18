"""Heuristic: does this scraped text look like an actual food menu?

The scraper's old gate ("more than N characters") happily accepted homepage
marketing copy with zero dishes. This scores text on statistical tells that
separate a real menu from a landing page, so ingestion can keep the best page
and flag restaurants where no real menu was found.

It is deliberately rule-based (fast, free, no API). It won't be perfect, but
it reliably rejects the obvious "Authentic Cuisine · Reserve Your Table"
homepages. Returns a MenuScore with a breakdown so the signal is explainable
(and tunable) rather than a black box.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Prices are the single strongest menu signal: "$9", "$12.50", "12.00".
_PRICE_RE = re.compile(r"(?:\$\s?\d{1,3}(?:\.\d{2})?)|(?:\b\d{1,3}\.\d{2}\b)")

# Cocktail/coffee menus often write "espresso martini - 15" or "Buns . . . 9":
# a bare 1-3 digit number at line end after a dash or dot leader. Anchored to
# the separator + line end so years, addresses, and "Suite 201" never match.
_TRAILING_PRICE_RE = re.compile(
    r"(?:\s[-–—]\s?|\.{2,}\s?|\.\s\.\s)(\d{1,3})(?:\.\d{2})?\s*$", re.M
)

# Menu-section headers. Their presence strongly implies a structured menu.
_SECTION_WORDS = (
    "appetizer",
    "appetizers",
    "starter",
    "starters",
    "entree",
    "entrees",
    "entrée",
    "mains",
    "main course",
    "sides",
    "side dishes",
    "salad",
    "salads",
    "soup",
    "soups",
    "dessert",
    "desserts",
    "beverage",
    "beverages",
    "drinks",
    "sandwiches",
    "burgers",
    "pizzas",
    "pasta",
    "small plates",
    "specialties",
    "à la carte",
    "a la carte",
)

# Food / ingredient / preparation words. Lots of these => menu-ish content.
# (Kept broad but food-specific; avoids generic words that appear anywhere.)
_FOOD_WORDS = (
    "chicken", "beef", "pork", "shrimp", "salmon", "tuna", "fish", "lamb",
    "bacon", "sausage", "turkey", "steak", "cheese", "mozzarella", "parmesan",
    "cheddar", "tomato", "onion", "garlic", "basil", "cilantro", "lettuce",
    "spinach", "mushroom", "pepper", "avocado", "cucumber", "rice", "noodle",
    "pasta", "bread", "bun", "tortilla", "beans", "lentil", "chickpea",
    "tofu", "hummus", "falafel", "curry", "sauce", "dressing", "vinaigrette",
    "grilled", "fried", "roasted", "baked", "sauteed", "sautéed", "crispy",
    "marinated", "braised", "smoked", "served with", "topped with", "side of",
    "vegan", "vegetarian", "gluten", "dairy", "egg", "cream", "butter",
    "wrap", "bowl", "plate", "taco", "burrito", "sandwich", "burger", "salad",
    "soup", "appetizer", "dessert", "espresso", "latte",
    # dessert-menu vocabulary — without it, a dessert PDF full of tortes
    # and ganache read as "few food words" and got rejected (The Chapman).
    "chocolate", "vanilla", "caramel", "cake", "cheesecake", "brownie",
    "cookie", "sorbet", "gelato", "ice cream", "custard", "pudding",
    "tart", "pie", "mousse", "ganache", "meringue", "sundae",
)


@dataclass
class MenuScore:
    score: float           # 0..1, higher = more menu-like
    is_menu: bool          # score >= threshold
    price_count: int
    food_word_hits: int
    section_hits: int
    short_line_ratio: float
    line_count: int
    reason: str            # short human-readable explanation


# A page must clear this to count as a real menu.
MENU_THRESHOLD = 0.45

# Gift-card / voucher storefront tells. Such pages carry several dollar
# amounts (the card denominations), which the price signal would otherwise
# read as a strong menu.
_GIFT_WORDS = (
    "gift card", "egift", "e-gift", "gift cards", "voucher",
    "check balance", "reload card", "recipient",
)


def _count_food_words(low: str) -> int:
    return sum(low.count(w) for w in _FOOD_WORDS)


def _count_sections(low: str) -> int:
    return sum(1 for w in _SECTION_WORDS if w in low)


def score_menu_text(text: str) -> MenuScore:
    """Score how menu-like `text` is, in [0, 1], with a breakdown."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    line_count = len(lines)
    low = text.lower()

    price_count = len(_PRICE_RE.findall(text)) + len(
        _TRAILING_PRICE_RE.findall(text)
    )
    food_hits = _count_food_words(low)
    section_hits = _count_sections(low)

    # Menus are lists of short items; marketing copy is longer prose. Ratio of
    # short-ish lines (a dish name / dish + description) to all lines.
    short_lines = sum(1 for ln in lines if 3 <= len(ln) <= 80)
    short_line_ratio = (short_lines / line_count) if line_count else 0.0

    # Normalize each signal into 0..1, then weight. Prices and sections are the
    # most trustworthy; food words scale with menu length; structure is a
    # supporting signal.
    price_sig = min(price_count / 8.0, 1.0)          # ~8 prices = clearly a menu
    food_sig = min(food_hits / 25.0, 1.0)            # many food words
    section_sig = min(section_hits / 3.0, 1.0)       # a few section headers
    struct_sig = short_line_ratio                    # already 0..1

    score = (
        0.40 * price_sig
        + 0.25 * food_sig
        + 0.20 * section_sig
        + 0.15 * struct_sig
    )

    # Menus WITHOUT printed prices are real (ice cream shops, "market
    # price" places, coffee menus). When there are no prices at all, score
    # on the remaining signals at full weight instead of forfeiting 40% —
    # a text dense with food words and dish-shaped lines still clears the
    # bar on their strength; thin marketing copy still can't (food alone
    # maxes at 0.40 < threshold, so structure/sections must corroborate).
    if price_count == 0:
        score = max(
            score,
            0.40 * food_sig + 0.30 * section_sig + 0.30 * struct_sig,
        )

    # Gift-card storefronts (Square gift pages etc.) are all prices and no
    # food — cap them below the menu bar no matter how "pricey" they look.
    gift_hits = sum(low.count(w) for w in _GIFT_WORDS)
    is_gift_page = gift_hits >= 3 and food_hits < 10
    if is_gift_page:
        score = min(score, 0.25)

    score = round(min(score, 1.0), 3)
    is_menu = score >= MENU_THRESHOLD

    if is_gift_page:
        return MenuScore(
            score=score,
            is_menu=False,
            price_count=price_count,
            food_word_hits=food_hits,
            section_hits=section_hits,
            short_line_ratio=round(short_line_ratio, 3),
            line_count=line_count,
            reason="looks like a gift-card/voucher page, not a menu",
        )

    if is_menu:
        reason = (
            f"{price_count} prices, {food_hits} food words, "
            f"{section_hits} menu sections"
        )
    else:
        bits = []
        if price_count == 0:
            bits.append("no prices")
        if food_hits < 5:
            bits.append("few food words")
        if section_hits == 0:
            bits.append("no menu sections")
        reason = "looks like homepage/marketing (" + ", ".join(bits or ["low signal"]) + ")"

    return MenuScore(
        score=score,
        is_menu=is_menu,
        price_count=price_count,
        food_word_hits=food_hits,
        section_hits=section_hits,
        short_line_ratio=round(short_line_ratio, 3),
        line_count=line_count,
        reason=reason,
    )
