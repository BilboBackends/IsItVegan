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
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper  # noqa: E402
from scraper import (  # noqa: E402
    ScrapeResult,
    _collect_headless,
    _extract_viguest_items,
    _find_menu_links,
    _find_viguest_category_urls,
    _finish,
    _is_single_section_url,
    _looks_menu_like,
    _score_viguest_site_match,
    _try_learned_context,
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


def test_known_ordering_host_does_not_require_menu_words():
    # Krungthep's Wix button only says "PICK UP" and its Toast URL ends /v3.
    html = _menu_html(
        [("PICK UP", "https://www.toasttab.com/krungthep-tea-time/v3")]
    )

    follow, hosts, tp_urls = _find_menu_links(html, "https://restaurant.test/")

    assert follow == []
    assert hosts == ["toasttab.com"]
    assert tp_urls == ["https://www.toasttab.com/krungthep-tea-time/v3"]


def test_ordering_host_match_requires_a_real_domain_boundary():
    html = _menu_html(
        [
            ("Continue", "https://toasttab.com.evil.test/restaurant"),
            ("Continue", "https://toasttab.com@evil.test/restaurant"),
        ]
    )

    _follow, hosts, tp_urls = _find_menu_links(html, "https://restaurant.test/")

    assert hosts == []
    assert tp_urls == []


def test_external_press_article_is_not_treated_as_a_menu():
    # Broad local hints such as "eat" and "sandwich" must not turn an
    # off-site press link into a menu candidate.
    html = _menu_html(
        [
            (
                "Eat This: Best Chicken Sandwich in Every State",
                "https://press.test/best-chicken-sandwich/",
            ),
            (
                "Restaurant opens a vegan kitchen",
                "https://press.test/opens-for-takeout-and-delivery/",
            ),
            ("View menu", "https://ordering.test/current-menu"),
        ]
    )

    _follow, _hosts, tp_urls = _find_menu_links(
        html, "https://restaurant.test/"
    )

    assert tp_urls == ["https://ordering.test/current-menu"]


def test_find_menu_links_reads_hostinger_serialized_buttons():
    # Clase Azul's Hostinger page stores "OUR MENU" / "ORDER ONLINE" buttons
    # inside escaped builder JSON, not normal anchors.
    pdf = (
        "https://claseazul.restaurant/wp-content/uploads/2025/08/"
        "Clase-Azul-Mexican-Cuisine-MM-R5495307-LR-ol-1.pdf"
    )
    html = f"""
    &quot;href&quot;:[0,&quot;{pdf}&quot;],
    &quot;content&quot;:[0,&quot;OUR MENU&quot;],
    &quot;href&quot;:[0,&quot;https://oo.viguest.com/?siteName=claseazulcape&quot;],
    &quot;content&quot;:[0,&quot;ORDER ONLINE&quot;]
    """
    follow, hosts, tp_urls = _find_menu_links(html, "https://classazulcapecoral.com/")
    assert follow == []
    assert "viguest.com" in hosts
    assert "https://oo.viguest.com/?siteName=claseazulcape" in tp_urls
    # PDF links are extracted by the PDF scraper, not sent to headless.
    assert not any(url.endswith(".pdf") for url in tp_urls)


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


def test_strong_menu_drops_weak_priceless_press_homepage():
    # Krungthep's homepage press headlines barely crossed the menu threshold
    # and contributed a bogus chicken-sandwich dish beside the real Toast menu.
    press_homepage = """
    top of page
    START MY ORDER
    KRUNGTHEP TEA TIME
    THAI TWIST SANDWICH AND TEA BAR
    START MY ORDER
    OPEN DINE IN PICK UP AND DELIVERY
    Visit Us
    Winter Park, FL
    OPENING HOURS
    Sun - Thu 11AM - 9PM
    Fri - Sat 11AM - 10PM
    Introducing our SISTER!
    PRESS AND REVIEW
    KrungThep Tea Time to Open Vegan Kitchen
    1/19/2021
    EATER ATLANTA: 20 Must-Try Sandwiches in Atlanta
    10/5/2020
    EATER ATLANTA: Thai Sandwich and Tea Bar Krungthep Tea Time Opens in Berkeley Park
    9/11/2020
    Eat This, Not That: The Best Chicken Sandwich in Every State
    01/10/2020
    bottom of page
    """.strip()
    assert scraper.score_menu_text(press_homepage).is_menu
    assert scraper.score_menu_text(press_homepage).score < 0.6

    result = _finish(
        "https://restaurant.test/",
        [
            ("https://restaurant.test/", press_homepage),
            ("https://order.toasttab.com/online/restaurant", MENU_TEXT_A),
        ],
        ["toasttab.com"],
        status_code=200,
        crawl_method="headless",
    )

    assert result.ok
    assert [url for url, _text in result.pages] == [
        "https://order.toasttab.com/online/restaurant"
    ]
    assert "Best Chicken Sandwich" not in result.text


def test_strong_menu_keeps_a_legitimate_priceless_section_page():
    sweets = "Sweets\n" + "\n".join(
        [
            "Coconut mango pudding",
            "Chocolate layer cake",
            "Strawberry shortcake",
            "Banana cream pie",
            "Lemon berry tart",
            "Peanut butter cookie",
            "Vanilla bean ice cream",
            "Caramel apple crisp",
            "Silken seasonal special",
            "House celebration selection",
            "Chef rotating confection",
            "Golden orchard favorite",
        ]
    )
    score = scraper.score_menu_text(sweets)
    assert score.is_menu and score.score < 0.6 and score.price_count == 0

    result = _finish(
        "https://restaurant.test/",
        [
            ("https://restaurant.test/", MENU_TEXT_A),
            ("https://restaurant.test/sweets", sweets),
        ],
        [],
        status_code=200,
    )

    assert result.ok
    assert "https://restaurant.test/sweets" in [url for url, _ in result.pages]
    assert "Coconut mango pudding" in result.text


def test_multiple_ordering_platform_copies_keep_the_strongest_catalog():
    toast_menu = MENU_TEXT_A + "\nChef special tofu platter $24.00"
    delivery_copy = MENU_TEXT_A.replace("grilled tofu", "tofu")

    result = _finish(
        "https://restaurant.test/",
        [
            ("https://order.toasttab.com/online/restaurant", toast_menu),
            ("https://www.ubereats.com/store/restaurant", delivery_copy),
        ],
        ["toasttab.com", "ubereats.com"],
        status_code=200,
        crawl_method="headless",
    )

    assert result.ok
    assert [url for url, _text in result.pages] == [
        "https://order.toasttab.com/online/restaurant"
    ]


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


def test_viguest_categories_are_found_from_ordering_landing():
    html = """
    <script>
    location.href =  '/Home/Merchandise?selected=164&name=Ceviches&siteName=claseazulcape&specialInstructions=True';
    location.href =  '/Home/Merchandise?selected=159&name=Appetizer&siteName=claseazulcape&specialInstructions=True';
    </script>
    """
    urls = _find_viguest_category_urls(
        html, "https://oo.viguest.com/?siteName=claseazulcape"
    )
    assert urls == [
        "https://oo.viguest.com/Home/Merchandise?selected=164&name=Ceviches&siteName=claseazulcape&specialInstructions=True",
        "https://oo.viguest.com/Home/Merchandise?selected=159&name=Appetizer&siteName=claseazulcape&specialInstructions=True",
    ]


def test_viguest_render_merchandise_calls_become_menu_items():
    html = """
    <script>
    renderMerchandise(0,"54","Aguachile", 17.5, "Lime-infused shrimp, onions, peppers, cucumber, jalape\\u00f1os, avocado, and lime juice.", "https://images.onepos.com/16065/item54.png", "", "#f5e85e", "#000000", "False");
    renderMerchandise(1,"199","Cabos Ceviche", 16.5, "Catch of the day shrimp, red onion, and cilantro.", "https://images.onepos.com/16065/item199.png", "", "#f5e85e", "#000000", "False");
    </script>
    """
    assert _extract_viguest_items(html) == [
        (
            "Aguachile",
            "Lime-infused shrimp, onions, peppers, cucumber, jalapeños, avocado, and lime juice.",
            "$17.50",
        ),
        (
            "Cabos Ceviche",
            "Catch of the day shrimp, red onion, and cilantro.",
            "$16.50",
        ),
    ]


def test_viguest_site_match_prefers_location_slug():
    home = "https://classazulcapecoral.com/"
    cape = "https://oo.viguest.com/?siteName=claseazulcape"
    fort_myers = "https://oo.viguest.com/?siteName=Claseazulfortmyers"
    assert _score_viguest_site_match(cape, home) > _score_viguest_site_match(
        fort_myers, home
    )


def test_headless_walks_seeded_viguest_categories_in_one_session(monkeypatch):
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
            if url == "https://classazulcapecoral.com/":
                return "<html><body>Clase Azul homepage</body></html>", None
            if url == "https://oo.viguest.com/?siteName=claseazulcape":
                return """
                <html><body>Menu Selections
                <script>
                location.href = '/Home/Merchandise?selected=164&name=Ceviches&siteName=claseazulcape&specialInstructions=True';
                </script>
                </body></html>
                """, None
            return f"<html><body><pre>{MENU_TEXT_A}</pre></body></html>", None

    monkeypatch.setattr(scraper, "RenderedSession", FakeSession)
    pages, _hosts = _collect_headless(
        "https://classazulcapecoral.com/",
        seed_urls=["https://oo.viguest.com/?siteName=claseazulcape"],
    )

    assert len(instances) == 1
    assert instances[0].calls == [
        "https://classazulcapecoral.com/",
        "https://oo.viguest.com/?siteName=claseazulcape",
        "https://oo.viguest.com/Home/Merchandise?selected=164&name=Ceviches&siteName=claseazulcape&specialInstructions=True",
    ]
    assert any("Dish number 3" in text for _url, text in pages)


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


# ---- PDF menu references (The Chapman pattern) ------------------------------

def test_find_pdf_urls_sees_viewer_scripts_and_relative_hrefs():
    from scraper import _find_pdf_urls

    html = """
    <html><body>
    <a href="/wp-content/uploads/2025/03/Dinner.pdf">Dinner</a>
    <script>var viewer = {file: "https://thechapman.com/uploads/Lunch26.pdf"};</script>
    <script>onclick="linkVersion('https://hillstone.com/menus/hillstone/Hillstone%20Winter%20Park%20Lunch.pdf')"</script>
    <a href="https://x.com/giftcard-menu.pdf">Gift</a>
    </body></html>
    """
    urls = _find_pdf_urls(html, "https://thechapman.com/lunch/")
    assert "https://thechapman.com/wp-content/uploads/2025/03/Dinner.pdf" in urls
    assert "https://thechapman.com/uploads/Lunch26.pdf" in urls
    assert "https://hillstone.com/menus/hillstone/Hillstone%20Winter%20Park%20Lunch.pdf" in urls
    # gift-card PDFs are junk, same as gift-card links
    assert not any("gift" in u for u in urls)


def test_fetch_retries_certificate_errors_without_verification(monkeypatch):
    class BadCertClient:
        def get(self, _url):
            raise scraper.httpx.ConnectError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
            )

    def fallback(url, timeout=25.0):
        return SimpleNamespace(
            status_code=200,
            headers={"content-type": "text/html"},
            text=f"<html><body>Menu from {url}</body></html>",
        )

    monkeypatch.setattr(scraper, "_fetch_without_cert_verification", fallback)

    result = scraper._fetch(BadCertClient(), "https://badcert.example/menu")

    assert result.error is None
    assert "Menu from https://badcert.example/menu" in result.html


def test_fetch_retries_intermittent_forbidden_response():
    # F&D Woodfired's edge sometimes returns two 403s before the same request works.
    responses = [
        SimpleNamespace(
            status_code=403,
            headers={"content-type": "text/html"},
            text="Forbidden",
            url="https://fd.example/menu",
        ),
        SimpleNamespace(
            status_code=403,
            headers={"content-type": "text/html"},
            text="Forbidden",
            url="https://fd.example/menu",
        ),
        SimpleNamespace(
            status_code=200,
            headers={"content-type": "text/html"},
            text="<html><body>Complete menu</body></html>",
            url="https://fd.example/menu",
        ),
    ]

    class IntermittentForbiddenClient:
        def __init__(self):
            self.calls = 0

        def get(self, _url):
            response = responses[self.calls]
            self.calls += 1
            return response

    client = IntermittentForbiddenClient()
    result = scraper._fetch(client, "https://fd.example/menu")

    assert client.calls == 3
    assert result.error is None
    assert "Complete menu" in result.html


def test_fetch_pdf_pages_retries_certificate_errors(monkeypatch):
    import pdf_menu

    class BadCertClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _url):
            raise scraper.httpx.ConnectError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"
            )

    monkeypatch.setattr(
        scraper.httpx, "Client", lambda *args, **kwargs: BadCertClient()
    )
    monkeypatch.setattr(
        scraper,
        "_fetch_without_cert_verification",
        lambda url, timeout=25.0: SimpleNamespace(
            status_code=200,
            content=b"%PDF-1.4 fake menu",
        ),
    )
    monkeypatch.setattr(
        pdf_menu,
        "extract_pdf_menu_text",
        lambda _content: "Dinner Menu\nVeggie Burger $17\nFrench Fries $7",
    )

    pages = scraper._fetch_pdf_pages(["https://hillstone.com/menu.pdf"])

    assert pages == [
        (
            "https://hillstone.com/menu.pdf",
            "Dinner Menu\nVeggie Burger $17\nFrench Fries $7",
        )
    ]


def test_letter_spaced_pdf_text_is_collapsed():
    # Some design-tool PDFs render kerning as one space per letter, two per
    # word gap — The Chapman's dessert menu extracted as "M e r i n g u e".
    from pdf_menu import _fix_letter_spacing

    spaced = (
        "D e s s e r t s\n"
        "S k y  H i g h  C i t r u s  M e r i n g u e  P i e\n"
        "G i n g e r s n a p  c r u s t ,  o r a n g e  z e s t  1 7\n"
        "Normal line stays untouched, even with $12 prices."
    )
    fixed = _fix_letter_spacing(spaced)
    assert "Desserts" in fixed
    assert "Sky High Citrus Meringue Pie" in fixed
    assert "Gingersnap crust, orange zest 17" in fixed
    assert "Normal line stays untouched, even with $12 prices." in fixed


def test_mediocre_learned_route_triggers_rediscovery():
    # A 0.49-score learned route (index blurbs) must not lock out discovery.
    from scraper import _try_learned_context

    result = _try_learned_context(
        "https://x.com/",
        {"menu_urls": ["https://x.com/menu"], "crawl_method": "http",
         "menu_score": 0.49, "char_count": 2283},
        timeout=5.0,
        use_headless=False,
    )
    # _collect_known_http would need network; the gate must reject BEFORE
    # accepting a mediocre result — a None here means rediscovery runs.
    assert result is None


def test_external_press_article_is_removed_from_learned_route(monkeypatch):
    # A previously poisoned high-score profile must self-heal instead of
    # walking the unrelated article forever.
    captured = []

    def fake_collect(urls, timeout, address=None):
        captured.extend(urls)
        return []

    monkeypatch.setattr(scraper, "_collect_known_http", fake_collect)
    result = _try_learned_context(
        "https://restaurant.test/",
        {
            "menu_urls": [
                "https://press.test/best-chicken-sandwich/",
                "https://restaurant.test/",
            ],
            "crawl_method": "http",
            "menu_score": 0.91,
            "char_count": 44_652,
        },
        timeout=5.0,
        use_headless=False,
    )

    assert result is None
    assert captured == ["https://restaurant.test/"]


def test_mediocre_pdf_learned_route_is_reused(monkeypatch):
    # The Strand's known-good PDF route scored below 0.75 because it has no
    # prices. That should still be retried before expensive rediscovery.
    calls = []

    def fake_collect(urls, timeout, address=None):
        calls.append((urls, timeout))
        return [("https://x.com/menu.pdf", MENU_TEXT_A * 3)]

    monkeypatch.setattr(scraper, "_collect_known_http", fake_collect)
    result = _try_learned_context(
        "https://x.com/",
        {"menu_urls": ["https://x.com/menu.pdf"], "crawl_method": "http",
         "menu_score": 0.67, "char_count": 1986},
        timeout=5.0,
        use_headless=False,
    )

    assert calls
    assert result is not None
    assert result.ok
    assert result.used_learned_context


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


# ---- redirect-aware page recording + daypart sibling probing ---------------
#
# Sixty Vines: /menu/winter-park silently 302s to /menu/winter-park/brunch,
# and the dinner/lunch tabs are client-side routes nothing links to. Pages
# recorded under the REQUESTED url hid the redirect from every single-section
# guard, so only the brunch menu was ever captured.

def _page(text: str) -> str:
    return "<html><body>" + text.replace("\n", "<br>") + "</body></html>"


class _FakeResponse(SimpleNamespace):
    pass


class _FakeClient:
    """Stands in for httpx.Client: routes -> (final_url, html) or 404."""

    def __init__(self, routes):
        self.routes = routes
        self.requested: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, url):
        self.requested.append(url)
        hit = self.routes.get(url)
        if hit is None:
            return _FakeResponse(
                status_code=404, headers={"content-type": "text/html"},
                text="not found", url=url,
            )
        final_url, html = hit
        return _FakeResponse(
            status_code=200, headers={"content-type": "text/html"},
            text=html, url=final_url,
        )


def test_fetch_records_final_redirected_url():
    client = _FakeClient({
        "https://sv.example/menu/winter-park": (
            "https://www.sv.example/menu/winter-park/brunch", _page(MENU_TEXT_A)
        ),
    })
    fetched = scraper._fetch(client, "https://sv.example/menu/winter-park")
    assert fetched.final_url == "https://www.sv.example/menu/winter-park/brunch"


def test_collect_http_probes_daypart_siblings_behind_redirect(monkeypatch):
    home = "https://sv.example/"
    routes = {
        home: (home, '<html><body><a href="/menu/winter-park">Menu</a></body></html>'),
        # The generic menu URL silently redirects to the brunch daypart page.
        "https://sv.example/menu/winter-park": (
            "https://www.sv.example/menu/winter-park/brunch", _page(MENU_TEXT_A)
        ),
        # Nothing links to /dinner — only a sibling probe can find it.
        "https://www.sv.example/menu/winter-park/dinner": (
            "https://www.sv.example/menu/winter-park/dinner", _page(MENU_TEXT_B)
        ),
    }
    client = _FakeClient(routes)
    monkeypatch.setattr(scraper.httpx, "Client", lambda *a, **k: client)

    pages, _tp, _tp_urls, _landing = scraper._collect_http(home, timeout=5.0)

    urls = [u for u, _ in pages]
    # The brunch capture is filed under where the redirect LANDED.
    assert "https://www.sv.example/menu/winter-park/brunch" in urls
    assert not any(u.rstrip("/") == "https://sv.example/menu/winter-park" for u in urls)
    # The unlinked dinner sibling was probed and captured.
    assert any("Evening plate 3" in text for _, text in pages)
    # Soft-404 siblings (lunch, breakfast, ...) were probed but not recorded.
    assert not any("not found" in text for _, text in pages)


def test_learned_route_self_heals_brunch_only_profile(monkeypatch):
    # The live Sixty Vines behavior for bot-ish user agents: the generic menu
    # URL serves ONE daypart's content directly — no redirect, no daypart in
    # any URL — and dinner exists only at an unlinked CHILD path. A stale
    # brunch-only learned profile must still discover it on refetch.
    routes = {
        "https://sv.example/menu/winter-park": (
            "https://www.sv.example/menu/winter-park", _page(MENU_TEXT_A)
        ),
        "https://www.sv.example/menu/winter-park/dinner": (
            "https://www.sv.example/menu/winter-park/dinner", _page(MENU_TEXT_B)
        ),
    }
    client = _FakeClient(routes)
    monkeypatch.setattr(scraper.httpx, "Client", lambda *a, **k: client)

    pages = scraper._collect_known_http(
        [
            "https://sv.example/menu/winter-park#structured-menu",
            "https://sv.example/menu/winter-park",
        ],
        timeout=5.0,
    )

    assert any("Evening plate 3" in text for _, text in pages)
    # Dead child probes (breakfast, wine, ...) were tried but not recorded.
    assert not any("not found" in text for _, text in pages)


def test_completeness_treats_structured_fragment_as_same_page():
    # A daypart page plus its #structured-menu pseudo-page is ONE capture —
    # it must still be rejected as a single-section menu.
    brunch = "https://www.sv.example/menu/winter-park/brunch"
    result = ScrapeResult(
        url=brunch,
        ok=True,
        is_menu=True,
        text=MENU_TEXT_A,
        char_count=len(MENU_TEXT_A),
        menu_score=0.8,
        pages=[(brunch, MENU_TEXT_A), (brunch + "#structured-menu", MENU_TEXT_A)],
    )
    checked = _validate_completeness(result)
    assert not checked.ok
    assert "only one menu section captured" in (checked.completeness_error or "")


def test_completeness_rejects_unresolved_dynamic_menu_loader():
    text = (
        "View our menus\nLoading Menu\n"
        "Happy Hour $5.00\nBrunch $34.00\n" + "Tacos and cocktails\n" * 100
    )
    result = ScrapeResult(
        url="https://restaurant.example/",
        ok=True,
        text=text,
        char_count=len(text),
        menu_score=0.9,
        is_menu=True,
        pages=[("https://restaurant.example/", text)],
        crawl_method="http",
    )

    checked = _validate_completeness(result)

    assert not checked.ok
    assert checked.completeness_error == "dynamic menu loader never resolved"


def test_square_online_catalog_mined_from_public_api(monkeypatch):
    # Nifty's Korean BBQ: a square.site landing shows 4 category tiles while
    # the platform's public products API carries all 84 items. Any page
    # embedding editmysite ids yields the catalog as a canonical pseudo-page.
    html = (
        '<script src="https://cdn3.editmysite.com/app/site.js"></script>'
        '<script>var cfg = {"user_id": "124870934",'
        '"site_id": "294717148660725181"};</script>'
        "<p>Ramen Ichiraku $16.00 Shop Now</p>"
    )
    scraper._square_catalog_text.cache_clear()
    calls = []

    def fake_catalog(user_id, site_id):
        calls.append((user_id, site_id))
        return "Ramen Tonkotsu  rich pork broth  $16.00\nBibimbap  $14.00"

    monkeypatch.setattr(scraper, "_square_catalog_text", fake_catalog)
    pages = scraper._pages_from_html("https://x.square.site/food", html)
    assert calls == [("124870934", "294717148660725181")]
    assert pages[-1][0] == "https://x.square.site/#square-catalog"
    assert "Ramen Tonkotsu" in pages[-1][1]

    # Non-Square pages never trigger the collector.
    pages = scraper._pages_from_html("https://x.com/", "<p>menu $5 fries</p>")
    assert all("#square-catalog" not in u for u, _ in pages)


def test_finish_dedupes_repeated_canonical_pseudo_pages():
    menu = "\n".join(f"Dish {i} with beans and rice ${i}.95" for i in range(1, 25))
    result = scraper._finish(
        "https://x.square.site/",
        [
            ("https://x.square.site/", "Shopping Cart\nCheckout"),
            ("https://x.square.site/#square-catalog", menu),
            ("https://x.square.site/#square-catalog", menu),
        ],
        [],
        status_code=200,
    )
    assert result.ok
    assert result.scraped_urls.count("https://x.square.site/#square-catalog") == 1


def test_apex_menu_probe_for_location_subdomains():
    # Chains list Yext location shells as each store's website; the real
    # menu lives at the brand apex (QDOBA / Jimmy John's / Subway pattern).
    assert scraper._apex_menu_url(
        "https://locations.qdoba.com/us/fl/cape-coral/537-sw-pine-island-rd"
    ) == "https://www.qdoba.com/menu"
    assert scraper._apex_menu_url(
        "https://restaurants.subway.com/united-states/fl/longwood/x"
    ) == "https://www.subway.com/menu"
    # Same-apex location paths and plain domains need no apex hop.
    assert scraper._apex_menu_url("https://www.firstwatch.com/locations/x") is None
    assert scraper._apex_menu_url("https://qdoba.com/menu") is None


def test_learned_route_on_location_shell_forces_rediscovery():
    # QDOBA: a route learned on locations.qdoba.com subpages (catering
    # scores 0.96!) must not lock out the apex-menu discovery probe.
    context = {
        "menu_urls": [
            "https://locations.qdoba.com/us/fl/cape-coral/537/catering",
        ],
        "crawl_method": "http",
        "menu_score": 0.96,
        "char_count": 5299,
    }
    assert scraper._try_learned_context(
        "https://locations.qdoba.com/us/fl/cape-coral/537",
        context, timeout=5.0, use_headless=False,
    ) is None


def test_server_error_pages_are_never_menu_candidates():
    # Domu's custom domain 500s every menu path with legacy-host CGI
    # boilerplate; its chrome scored 0.225 and polluted the crawl.
    error_html = (
        "<html><body><h1>Internal Server Error</h1><p>The server "
        "encountered an internal error or misconfiguration and was unable "
        "to complete your request.</p></body></html>"
    )
    assert scraper._pages_from_html("https://x.com/menu", error_html) == []
    # A real page mentioning an error in prose is not boilerplate.
    real = "<html><body>" + "Falafel $9 " * 300 + "</body></html>"
    assert scraper._pages_from_html("https://x.com/menu", real) != []


def test_cross_domain_canonical_yields_twin_root():
    html = '<link rel="canonical" href="https://domufl.squarespace.com">'
    assert scraper._canonical_cross_domain_root(
        html, "https://www.domufl.com/"
    ) == "https://domufl.squarespace.com"
    # Same-host canonicals and relative hrefs are not a twin signal.
    assert scraper._canonical_cross_domain_root(
        '<link rel="canonical" href="https://www.domufl.com/">',
        "https://www.domufl.com/",
    ) is None
    assert scraper._canonical_cross_domain_root(
        '<link rel="canonical" href="/here">', "https://www.domufl.com/"
    ) is None
