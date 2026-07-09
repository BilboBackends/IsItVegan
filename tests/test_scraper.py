"""Regression tests for the scraper's pure logic — no network, no browser.

Each test pins a failure mode we actually shipped and then fixed. If one of
these breaks, a real class of restaurant menus breaks with it:

- word-boundary hint matching       (followed /m/create-account via "eat")
- section-name menu links           (missed "Sandwiches"/"Salads" nav)
- multi-page keep in _finish        (stored only /breakfast, dropped lunch)
- third-party link detection        (Pickles: menu on activemenus.com)
- social-profile guard              (stored Instagram feed as a menu)
- marketing-copy rejection          (homepage with section names, no dishes)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper  # noqa: E402
from scraper import (  # noqa: E402
    ScrapeResult,
    _collect_headless,
    _find_menu_links,
    _finish,
    _is_single_section_url,
    _looks_menu_like,
    _validate_completeness,
    scrape_menu_text,
)

# ---- fixtures -------------------------------------------------------------

MENU_TEXT_A = "\n".join(
    ["Lunch Menu", "Appetizers"]
    + [f"Dish number {i} with grilled tofu and rice\n${i}.95" for i in range(1, 15)]
)

MENU_TEXT_B = "\n".join(
    ["Dinner Menu", "Entrees"]
    + [f"Evening plate {i} with roasted mushroom pasta\n${i + 10}.50" for i in range(1, 15)]
)

MARKETING_TEXT = """
Welcome to Our Restaurant
Authentic Cuisine, One Bite At A Time
Serving up classic hand-crafted favorites with the freshest ingredients.
Reserve your table today. Follow us on social media.
Our story began in 1985 when our founder arrived with a dream.
""" * 3


def _menu_html(links: list[tuple[str, str]]) -> str:
    anchors = "\n".join(f'<a href="{href}">{text}</a>' for text, href in links)
    return f"<html><body><nav>{anchors}</nav><p>hello</p></body></html>"


# ---- link finding ---------------------------------------------------------

def test_hint_matching_uses_word_boundaries():
    # "eat" must not match "create" — this once followed /m/create-account.
    assert not _looks_menu_like("Create Account", "/m/create-account")
    assert _looks_menu_like("Eat With Us", "/eat")


def test_section_names_count_as_menu_links():
    # Sites with per-section pages label nav "Sandwiches", never "menu".
    assert _looks_menu_like("Sandwiches", "/sandwiches")
    assert _looks_menu_like("Salads", "/salads")
    assert _looks_menu_like("Desserts & Beverages", "/deserts-%26-beverages")


def test_social_links_never_followed():
    assert not _looks_menu_like("Menu", "https://www.instagram.com/ourfood")
    assert not _looks_menu_like("Order", "https://facebook.com/ourpage")


def test_find_menu_links_separates_third_party():
    html = _menu_html(
        [
            ("Menu", "/menu"),
            ("Order Online", "https://pickles.activemenus.com/glue/menu/1"),
            ("Order Toast", "https://order.toasttab.com/online/some-place"),
            ("Instagram", "https://instagram.com/place"),
        ]
    )
    follow, hosts, tp_urls = _find_menu_links(html, "https://example.com/")
    assert follow == ["https://example.com/menu"]
    assert "toasttab.com" in hosts
    assert "activemenus.com" in hosts
    assert any("activemenus.com" in u for u in tp_urls)
    assert not any("instagram" in u for u in tp_urls)


def test_single_section_url_detection():
    assert _is_single_section_url("https://x.com/breakfast")
    assert _is_single_section_url("https://x.com/menu/sandwiches/")
    assert not _is_single_section_url("https://x.com/menu")
    assert not _is_single_section_url("https://x.com/")


# ---- page keeping (_finish) ------------------------------------------------

def test_finish_keeps_all_menu_pages_combined():
    # The original bug: only the best page was kept, dropping lunch/dinner.
    pages = [
        ("https://x.com/", MARKETING_TEXT),
        ("https://x.com/lunch", MENU_TEXT_A),
        ("https://x.com/dinner", MENU_TEXT_B),
    ]
    result = _finish("https://x.com/", pages, [], status_code=200)
    assert result.ok
    kept_urls = [u for u, _ in result.pages]
    assert "https://x.com/lunch" in kept_urls
    assert "https://x.com/dinner" in kept_urls
    assert "Dish number 3" in result.text and "Evening plate 3" in result.text


def test_finish_dedupes_contained_pages():
    # A landing page embedding the same menu as /menu must not double it.
    pages = [
        ("https://x.com/", MENU_TEXT_A),
        ("https://x.com/menu", MENU_TEXT_A + "\nplus one more line $9.99"),
    ]
    result = _finish("https://x.com/", pages, [], status_code=200)
    assert result.ok
    assert len(result.pages) == 1


def test_finish_rejects_marketing_copy():
    result = _finish(
        "https://x.com/", [("https://x.com/", MARKETING_TEXT)], [], status_code=200
    )
    assert not result.ok
    assert not result.pages


def test_single_section_result_cannot_be_learned_as_complete_menu():
    result = _validate_completeness(
        _finish(
            "https://x.com/",
            [("https://x.com/specials", MENU_TEXT_A)],
            [],
            status_code=200,
            crawl_method="headless",
        )
    )
    assert not result.ok
    assert "only one menu section" in (result.completeness_error or "")


def test_structured_multicategory_payload_overrides_section_url_warning():
    text = "[structured-menu products=25 categories=4]\n" + MENU_TEXT_A
    result = _validate_completeness(
        _finish(
            "https://x.com/",
            [("https://x.com/menu/entrees", text)],
            [],
            status_code=200,
            crawl_method="headless",
        )
    )
    assert result.ok
    assert result.structured_item_count == 25
    assert result.structured_category_count == 4


def test_headless_link_following_reuses_one_browser_session(monkeypatch):
    instances = []

    class FakeSession:
        def __init__(self):
            self.calls = []
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def fetch(self, url, **kwargs):
            self.calls.append(url)
            if len(self.calls) == 1:
                return '<html><body><a href="/menu">Menu</a></body></html>', None
            return f"<html><body><pre>{MENU_TEXT_A}</pre></body></html>", None

    monkeypatch.setattr(scraper, "RenderedSession", FakeSession)
    pages, _hosts = _collect_headless("https://x.com/location/1")

    assert len(instances) == 1
    assert instances[0].calls == [
        "https://x.com/location/1",
        "https://x.com/menu",
    ]
    assert any("Dish number 3" in text for _url, text in pages)


def test_headless_stops_after_complete_structured_landing_payload(monkeypatch):
    instances = []

    class FakeSession:
        def __init__(self):
            self.calls = []
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def fetch(self, url, **kwargs):
            self.calls.append(url)
            html = (
                '<html><body><a href="/menu">Menu</a><pre>'
                "[structured-menu products=40 categories=6]\n"
                f"{MENU_TEXT_A}</pre></body></html>"
            )
            return html, None

    monkeypatch.setattr(scraper, "RenderedSession", FakeSession)
    pages, _hosts = _collect_headless("https://x.com/location/1")

    assert len(pages) == 1
    assert instances[0].calls == ["https://x.com/location/1"]


# ---- menu scoring: the Sampaguita failure pair ------------------------------

GIFT_CARD_TEXT = """
Sampaguita Ice Cream LLC
Buy gift card
Reload card
Check balance
Give the Perfect Gift
Get a voucher for yourself or gift one to a friend
Send a gift card to one recipient or make a bulk purchase
eGift card amount
$10.00
Pay $10.00
$25.00
Pay $25.00
$50.00
Pay $45.00
$100.00
Pay $90.00
"""

PRICELESS_FLAVOR_MENU = "\n".join(
    ["Menu", "Handmade Flavors", "Desserts"]
    + [
        line
        for i in range(1, 16)
        for line in (
            f"Flavor Number {i} Supreme",
            f"Vanilla ice cream with roasted mango chunks, toasted rice "
            f"crunch and coconut cream swirl number {i}.",
            "*Contains dairy, egg, gluten",
        )
    ]
)


def test_gift_card_page_is_not_a_menu_despite_prices():
    # squareup.com/gift pages carry card denominations that read as prices;
    # one was stored as Sampaguita's menu while the real menu was rejected.
    from menu_score import score_menu_text

    result = score_menu_text(GIFT_CARD_TEXT)
    assert not result.is_menu
    assert "gift" in result.reason


def test_priceless_menu_with_dense_food_content_is_kept():
    # Ice cream shops and "market price" places print no prices; food-word
    # density plus dish-shaped lines must be able to carry the score alone.
    from menu_score import score_menu_text

    result = score_menu_text(PRICELESS_FLAVOR_MENU)
    assert result.price_count == 0
    assert result.is_menu, f"score {result.score}: {result.reason}"


def test_gift_links_never_followed():
    assert not _looks_menu_like(
        "Gift Cards", "https://squareup.com/gift/MLZ65N27SASE0/order"
    )
    assert not _looks_menu_like("Order eGift Voucher", "/gift-cards")


# ---- scrape_menu_text entry points (no network) ----------------------------

def test_social_profile_website_fails_fast():
    # Google sometimes lists Instagram as the website; there is no menu there.
    result = scrape_menu_text("https://www.instagram.com/death.in.the.afternoon_/")
    assert isinstance(result, ScrapeResult)
    assert not result.ok
    assert "social profile" in (result.error or "")


def test_mock_html_path_extracts_menu():
    html = "<html><body>" + MENU_TEXT_A.replace("\n", "<br>") + "</body></html>"
    result = scrape_menu_text("https://x.com/", mock_html=html)
    assert result.ok
    assert result.menu_score > 0.45
