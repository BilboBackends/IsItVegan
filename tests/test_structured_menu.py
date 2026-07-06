"""Tests for structured-menu extraction (the partial-menu deep-dive fixes).

Real failure modes pinned here:
- Popmenu (F&D Cantina): visible page = 990 chars / 1 section; the FULL menu
  (76 items) lives in schema.org JSON-LD in the static HTML.
- Chuan Fu's ordering platform: visible list is virtualized (no snapshot has
  every dish); the full menu is escaped JSON inside inline scripts.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import _pages_from_html  # noqa: E402
from structured_menu import (  # noqa: E402
    extract_client_state_menu,
    extract_embedded_menu_text,
    extract_jsonld_menu_text,
    extract_structured_menu_text,
)


def _jsonld_menu_html(n_items: int = 10) -> str:
    menu = {
        "@type": "Menu",
        "name": "Dinner",
        "hasMenuSection": [
            {
                "@type": "MenuSection",
                "name": "VEGETARIAN & VEGAN",
                "hasMenuItem": [
                    {
                        "@type": "MenuItem",
                        "name": f"Veggie Dish {i}",
                        "description": "with cilantro crema",
                        "offers": {"@type": "Offer", "price": "12.5"},
                    }
                    for i in range(n_items)
                ],
            }
        ],
    }
    return (
        "<html><head><script type='application/ld+json'>"
        + json.dumps(menu)
        + "</script></head><body><p>Load More Content</p></body></html>"
    )


def _embedded_menu_html(n_items: int = 10) -> str:
    # The double-escaped shape captured from a real ordering platform:
    # JSON serialized inside a JS string inside a <script>.
    items = ",".join(
        '{\\"menuItemId\\":%d,\\"menuItemName\\":\\"Dish %d 锅贴\\",'
        '\\"menuItemDesc\\":\\"spicy\\",\\"menuItemPrice\\":%d.99}' % (i, i, i + 5)
        for i in range(n_items)
    )
    padding = "x" * 2000  # extractor fast-rejects tiny scripts
    return (
        f"<html><body><script>var s = \"{{\\\"menuItems\\\":[{items}]}}\";"
        f"// {padding}</script></body></html>"
    )


def test_jsonld_menu_renders_sections_and_prices():
    text = extract_jsonld_menu_text(_jsonld_menu_html())
    assert text is not None
    assert "VEGETARIAN & VEGAN" in text
    assert "Veggie Dish 3 — with cilantro crema ($12.5)" in text


def test_jsonld_below_item_threshold_is_rejected():
    assert extract_jsonld_menu_text(_jsonld_menu_html(n_items=3)) is None


def test_embedded_escaped_json_menu_is_mined():
    text = extract_embedded_menu_text(_embedded_menu_html())
    assert text is not None
    assert "Dish 4 锅贴 — spicy ($9.99)" in text


def test_embedded_below_item_threshold_is_rejected():
    assert extract_embedded_menu_text(_embedded_menu_html(n_items=4)) is None


def test_jsonld_wins_over_script_mining():
    html = _jsonld_menu_html() + _embedded_menu_html()
    text = extract_structured_menu_text(html)
    assert "VEGETARIAN & VEGAN" in text


def test_pages_from_html_adds_structured_pseudo_page():
    pages = _pages_from_html("https://x.com/menu", _jsonld_menu_html())
    urls = [u for u, _ in pages]
    assert urls == ["https://x.com/menu", "https://x.com/menu#structured-menu"]
    assert "Veggie Dish 1" in pages[1][1]


def test_pages_from_html_plain_page_has_no_pseudo_page():
    pages = _pages_from_html("https://x.com/", "<html><body>hi</body></html>")
    assert len(pages) == 1


def test_rendered_client_state_extracts_nested_products_and_calories():
    products = [
        {
            "id": f"prod{i}",
            "displayName": f"Pasta {i}",
            "description": "Tomato, basil, and garlic",
            "price": {"formattedPrice": f"${10 + i}.99", "value": 10 + i + 0.99},
            "nutrition": {"cal": f"{500 + i * 10} cal"},
        }
        for i in range(10)
    ]
    # Product 0 is referenced in a second category; stable ids must dedupe it.
    payload = {
        "response": {
            "data": {
                "categories": [
                    {"displayName": "Entrees", "products": products},
                    {"displayName": "Favorites", "products": [products[0]]},
                ]
            }
        }
    }

    menu = extract_client_state_menu([json.dumps(payload)])

    assert menu is not None
    assert menu.item_count == 10
    assert menu.category_count == 1  # duplicate-only Favorites adds no new record
    assert "Pasta 3 — Tomato, basil, and garlic ($13.99) [530 cal]" in menu.text
    assert menu.text.count("Pasta 0") == 1


def test_rendered_client_state_rejects_configuration_noise():
    payload = {
        "settings": [
            {"displayName": f"Setting {i}", "description": "UI option"}
            for i in range(20)
        ]
    }
    assert extract_client_state_menu([json.dumps(payload)]) is None
