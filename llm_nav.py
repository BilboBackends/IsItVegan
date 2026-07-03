"""LLM-assisted menu navigation (cheap model).

Our keyword link-finder (scraper._find_menu_links) only matches literal words
like "menu"/"food". It misses links labeled "See what's cooking", "Bill of
Fare", "Order Now", or image-only nav. This module asks a small, cheap Claude
model — Haiku — to *choose which link is the menu*, given the page's candidate
links. It does NOT read or classify the menu (that's BeautifulSoup + Phase 3);
picking a link is an easy task a small model handles well and cheaply.

Two tiers:
1. choose_menu_link_from_text — feed candidate links (anchor text + href),
   Haiku returns the best menu URL. Tiny token cost.
2. choose_menu_link_from_screenshot — vision fallback for image-only / unlabeled
   nav: send a screenshot, Haiku returns the visible link text to click.

Both degrade gracefully: no API key or SDK error -> return None, and the
caller falls back to whatever it had.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from config import settings

# Small, cheap, fast — right for "which of these links is the menu".
_MODEL = "claude-haiku-4-5"

# Cap how many candidate links we send, to bound tokens.
_MAX_CANDIDATES = 40


@dataclass
class NavChoice:
    url: str | None = None          # chosen menu URL (text tier)
    link_text: str | None = None    # chosen link text to click (vision tier)
    reason: str | None = None


def _client():
    """Return an Anthropic client, or None if unusable (no key / import fail)."""
    if not settings.anthropic_api_key:
        return None
    try:
        import anthropic

        return anthropic.Anthropic(api_key=settings.anthropic_api_key)
    except Exception:
        return None


def choose_menu_link_from_text(candidates: list[dict]) -> NavChoice:
    """Ask Haiku which candidate link points at the food menu.

    candidates: [{"text": <anchor text>, "url": <absolute href>}, ...]
    Returns NavChoice with .url set to the chosen URL, or None if none fit.
    """
    client = _client()
    if client is None or not candidates:
        return NavChoice()

    trimmed = candidates[:_MAX_CANDIDATES]
    listing = "\n".join(
        f'{i}. text="{c.get("text", "")}" url={c.get("url", "")}'
        for i, c in enumerate(trimmed)
    )

    schema = {
        "type": "object",
        "properties": {
            "index": {
                "type": "integer",
                "description": "Index of the link most likely to lead to the "
                "restaurant's food menu, or -1 if none of them do.",
            },
            "reason": {"type": "string"},
        },
        "required": ["index", "reason"],
        "additionalProperties": False,
    }

    prompt = (
        "You are helping find a restaurant's food menu on its website. "
        "Below is a numbered list of links from the page. Pick the ONE link "
        "most likely to lead to the actual food/drink menu (dishes and prices). "
        "Prefer links about menu/food/dinner/lunch/order over About, Contact, "
        "Reservations, Careers, Gift Cards, or social media. If none plausibly "
        "lead to a menu, return index -1.\n\n"
        f"Links:\n{listing}"
    )

    try:
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=256,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(text)
    except Exception as exc:
        return NavChoice(reason=f"llm error: {exc}")

    idx = data.get("index", -1)
    if not isinstance(idx, int) or idx < 0 or idx >= len(trimmed):
        return NavChoice(reason=data.get("reason", "no suitable link"))
    return NavChoice(url=trimmed[idx].get("url"), reason=data.get("reason"))


def choose_menu_link_from_screenshot(
    png_bytes: bytes, visible_link_texts: list[str]
) -> NavChoice:
    """Vision fallback: given a screenshot, pick which visible link to click.

    Used when a page's nav is image-only / unlabeled so text links are useless.
    Returns NavChoice with .link_text set to the text the caller should click.
    """
    client = _client()
    if client is None or not png_bytes:
        return NavChoice()

    import base64

    b64 = base64.standard_b64encode(png_bytes).decode("utf-8")
    options = "\n".join(f"- {t}" for t in visible_link_texts[:_MAX_CANDIDATES])

    schema = {
        "type": "object",
        "properties": {
            "link_text": {
                "type": "string",
                "description": "Exact visible text of the nav item to click to "
                "reach the food menu, or empty string if none is visible.",
            },
            "reason": {"type": "string"},
        },
        "required": ["link_text", "reason"],
        "additionalProperties": False,
    }

    prompt = (
        "This is a screenshot of a restaurant website. Identify the navigation "
        "item a user would click to see the food menu (dishes and prices). "
        "Return its exact visible text. If a list of link texts is given, "
        "choose from it. If no menu link is visible, return an empty string.\n\n"
        f"Known link texts:\n{options or '(none provided)'}"
    )

    try:
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=256,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(text)
    except Exception as exc:
        return NavChoice(reason=f"llm vision error: {exc}")

    link_text = (data.get("link_text") or "").strip()
    return NavChoice(link_text=link_text or None, reason=data.get("reason"))
