"""Extract menus from structured data embedded in restaurant pages.

The deep-dive that produced this module found that ordering platforms which
render almost nothing as DOM text still ship their ENTIRE menu inside the
page as machine-readable data:

- Popmenu (F&D Cantina): full schema.org Menu in `application/ld+json` —
  76 items with names, descriptions, and prices in the static HTML while the
  visible page lazy-loads two sections and 990 chars.
- Chuan Fu's ordering platform: complete menu as escaped JSON inside inline
  scripts (menuItems -> menuItemName/menuItemDesc/menuItemPrice), while the
  visible list is VIRTUALIZED so no DOM snapshot ever holds every dish.

So: before trusting rendered text, mine the page for structured menus and
render them as clean menu text lines ("Name — description ($9.99)"). The
downstream classifier reads text, so this needs no schema changes anywhere.
"""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

# A mined menu must clear this to be trusted — a couple of stray name/price
# pairs (e.g. a gift-card product) is not a menu.
MIN_STRUCTURED_ITEMS = 8

# name-ish / price-ish key aliases seen across ordering platforms.
_NAME_KEYS = r"(?:menuItemName|itemName|item_name|dishName|productName)"
_PRICE_KEYS = r"(?:menuItemPrice|defaultUnitPrice|unitPrice|basePrice|price_amount|price)"

# Tolerates plain ("key") and escaped (\"key\") JSON embedded in JS strings.
_PAIR_RE = re.compile(
    r"\\{0,2}\"" + _NAME_KEYS + r"\\{0,2}\"\s*:\s*\\{0,2}\"(?P<name>(?:[^\"\\]|\\.)+?)\\{0,2}\""
    r"(?P<between>.{0,700}?)"
    r"\\{0,2}\"" + _PRICE_KEYS + r"\\{0,2}\"\s*:\s*\\{0,2}\"?(?P<price>\d{1,4}(?:\.\d{1,2})?)",
    re.DOTALL,
)
_DESC_RE = re.compile(
    r"\\{0,2}\"(?:menuItemDesc|itemDesc|description|desc)\\{0,2}\"\s*:\s*\\{0,2}\"(?P<desc>(?:[^\"\\]|\\.)+?)\\{0,2}\""
)


def _unescape(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace('\\"', '"').replace("\\\\", "\\")


def _format_item(name: str, description: str | None, price) -> str:
    line = name.strip()
    if description:
        # Regex-mined descriptions from empty JSON fields can degrade to
        # stray quote/comma fragments — only real words qualify.
        description = " ".join(str(description).split()).strip(" \"',\\")
        if len(description) >= 3:
            line += f" — {description}"
    if price not in (None, ""):
        line += f" (${price})"
    return line


# ---------------------------------------------------------------------------
# schema.org JSON-LD (Popmenu, BentoBox, many SEO-conscious sites)
# ---------------------------------------------------------------------------

def _iter_jsonld_objects(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_jsonld_objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_jsonld_objects(value)


def _menu_sections(menu_obj: dict) -> list[dict]:
    sections = menu_obj.get("hasMenuSection") or []
    if isinstance(sections, dict):
        sections = [sections]
    # Some emitters nest the section list one level deep.
    if sections and isinstance(sections[0], list):
        sections = [s for group in sections for s in group]
    return [s for s in sections if isinstance(s, dict)]


def extract_jsonld_menu_text(html: str) -> str | None:
    """Render every schema.org Menu found in ld+json blocks as menu text."""
    soup = BeautifulSoup(html, "html.parser")
    lines: list[str] = []
    item_count = 0
    seen: set[str] = set()

    for script in soup.find_all("script", {"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if "@type" not in raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for obj in _iter_jsonld_objects(data):
            if obj.get("@type") not in ("Menu", "hasMenu"):
                continue
            menu_name = obj.get("name")
            if menu_name:
                lines.append(f"== {menu_name} ==")
            for section in _menu_sections(obj):
                section_name = section.get("name")
                if section_name:
                    lines.append(f"{section_name}:")
                items = section.get("hasMenuItem") or []
                if isinstance(items, dict):
                    items = [items]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    name = (item.get("name") or "").strip()
                    if not name:
                        continue
                    offers = item.get("offers") or {}
                    if isinstance(offers, list):
                        offers = offers[0] if offers else {}
                    price = offers.get("price") if isinstance(offers, dict) else None
                    line = _format_item(name, item.get("description"), price)
                    if line not in seen:
                        seen.add(line)
                        lines.append(line)
                        item_count += 1

    if item_count < MIN_STRUCTURED_ITEMS:
        return None
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Generic embedded-JSON mining (ordering-platform SPA state in <script>s)
# ---------------------------------------------------------------------------

def extract_embedded_menu_text(html: str) -> str | None:
    """Mine inline scripts for (item name, price) pairs, however escaped.

    Platform-agnostic on purpose: ordering systems churn, but they all ship
    their menu state with SOME name key next to SOME price key. Only trusts
    the result when it finds a menu's worth of items.
    """
    soup = BeautifulSoup(html, "html.parser")
    lines: list[str] = []
    seen: set[str] = set()

    for script in soup.find_all("script"):
        body = script.string or ""
        if len(body) < 2_000 or "rice" not in body:  # p/Price fast reject
            continue
        for match in _PAIR_RE.finditer(body):
            name = _unescape(match.group("name")).strip()
            if not name or len(name) > 120:
                continue
            between = match.group("between")
            desc_match = _DESC_RE.search(between)
            description = _unescape(desc_match.group("desc")) if desc_match else None
            line = _format_item(name, description, match.group("price"))
            if line not in seen:
                seen.add(line)
                lines.append(line)

    if len(lines) < MIN_STRUCTURED_ITEMS:
        return None
    return "\n".join(lines)


def extract_structured_menu_text(html: str) -> str | None:
    """Best structured menu found in the page, or None.

    JSON-LD wins when both exist — it's intentional, curated markup;
    the script miner is the heuristic fallback.
    """
    return extract_jsonld_menu_text(html) or extract_embedded_menu_text(html)
