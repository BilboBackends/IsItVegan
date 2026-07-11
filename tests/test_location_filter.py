"""Location filtering for multi-location brand sites (the Pizza Bruno bug)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from location_filter import (
    LocationUrlFilter,
    address_tokens,
    area_from_address,
    filter_location_urls,
    looks_location_specific,
    matches_location,
    strong_location_match,
)

MAITLAND_ADDR = "360 E Horatio Ave #300, Maitland, FL 32751, USA"

# The exact URL set the scraper stored for Pizza Bruno Maitland (id 51).
BRUNO_URLS = [
    "https://www.pizzabrunofl.com/menu/pizza-bruno-curry-ford-3990-curry-ford-road",
    "https://www.pizzabrunofl.com/order/pizza-bruno-curry-ford-3990-curry-ford-road",
    "https://www.pizzabrunofl.com/order/pizza-bruno-maitland-360-e-horatio-ave-suite-500",
    "https://www.pizzabrunofl.com/menu/pizza-bruno-maitland-360-e-horatio-ave-suite-500",
    "https://www.pizzabrunofl.com/order/pizza-bruno-maitland-360-e-horatio-ave-suite-500/item-12-wings_7008d881-9325-4f0c-8faf-dbbf5637e86d",
    "https://www.pizzabrunofl.com/menu/pizza-bruno-college-park-2429-edgewater-dr",
]


def test_address_tokens_places_format():
    tokens = address_tokens(MAITLAND_ADDR)
    assert tokens.street_number == "360"
    assert tokens.street_words == {"horatio"}
    assert tokens.city_words == {"maitland"}


def test_address_tokens_multiword_city():
    tokens = address_tokens("565 W Fairbanks Ave, Winter Park, FL 32789, USA")
    assert tokens.city_words == {"winter", "park"}
    assert tokens.street_words == {"fairbanks"}


def test_address_tokens_empty():
    assert not address_tokens(None)
    assert not address_tokens("")


def test_matches_location_by_city_street_and_number():
    tokens = address_tokens(MAITLAND_ADDR)
    assert matches_location(BRUNO_URLS[3], tokens)  # maitland + 360 + horatio
    assert not matches_location(BRUNO_URLS[0], tokens)  # curry ford
    assert not matches_location(BRUNO_URLS[5], tokens)  # college park


def test_city_match_alone_cannot_anchor():
    # The Glass Knife regression: every URL carries ?location=park-avenue and
    # a menu-category slug contains the CITY name ("cafe-in-winter-park").
    # City words are too generic to anchor on — nothing may be dropped.
    urls = [
        "https://theglassknife.com/menu-category/happy-hour/?location=park-avenue",
        "https://theglassknife.com/menu-category/cake/?location=park-avenue",
        "https://theglassknife.com/menu-category/cafe-in-winter-park/?location=park-avenue",
    ]
    assert filter_location_urls(urls, "276 S Orlando Ave, Winter Park, FL 32789, USA") == urls


def test_street_name_anchor_requires_road_suffix_adjacency():
    tokens = address_tokens("212 N Park Ave, Winter Park, FL 32789, USA")
    # "park-avenue" = the street; "college-park-2429-edgewater-dr" is a
    # neighborhood slug that merely contains the word "park".
    assert strong_location_match(
        "https://x.com/menu?location=park-avenue", tokens
    )
    assert not strong_location_match(
        "https://x.com/menu/brand-college-park-2429-edgewater-dr", tokens
    )


def test_fused_city_token_matches():
    tokens = address_tokens("215 S Orlando Ave, Winter Park, FL 32789, USA")
    assert matches_location("https://x.com/locations/winterpark/", tokens)


def test_looks_location_specific_address_slugs():
    # street number followed by street words + suffix
    assert looks_location_specific(BRUNO_URLS[0])  # 3990-curry-ford-road
    assert looks_location_specific(BRUNO_URLS[5])  # 2429-edgewater-dr
    assert looks_location_specific(
        "https://example.com/locations/winter-park/menu"
    )
    # Plain menu pages and item ids must NOT look location-specific.
    assert not looks_location_specific("https://example.com/menu")
    assert not looks_location_specific("https://example.com/menu/dinner")
    assert not looks_location_specific(
        "https://example.com/order/item-12-wings_7008d881-9325"
    )
    # WP upload-date paths: "st" after /2026/05/ is Saint, not Street.
    assert not looks_location_specific(
        "https://x.com/wp-content/uploads/2026/05/Anejo-St-Johns-Menus-2026.pdf"
    )
    # Delivery platforms route everything under /store/<name> — generic.
    assert not looks_location_specific(
        "https://www.ubereats.com/store/boku-sushi-%26-grill/6vA7oD5sQ4"
    )


def test_pizza_bruno_filtered_to_maitland_only():
    kept = filter_location_urls(BRUNO_URLS, MAITLAND_ADDR)
    assert kept == [u for u in BRUNO_URLS if "maitland" in u]


def test_other_location_record_keeps_its_own_pages():
    # The same site scraped for a hypothetical Curry Ford record.
    kept = filter_location_urls(
        BRUNO_URLS, "3990 Curry Ford Rd, Orlando, FL 32806, USA"
    )
    assert kept == [u for u in BRUNO_URLS if "curry-ford" in u]


def test_safety_valve_shared_menu_untouched():
    # No candidate names our address -> nothing is dropped, even though one
    # URL looks address-like (we can't prove which page is ours).
    urls = [
        "https://example.com/menu",
        "https://example.com/menu/dinner",
        "https://example.com/menu/other-town-99-main-st",
    ]
    assert filter_location_urls(urls, MAITLAND_ADDR) == urls


def test_safety_valve_no_address():
    assert filter_location_urls(BRUNO_URLS, None) == BRUNO_URLS
    assert filter_location_urls(BRUNO_URLS, "") == BRUNO_URLS


def test_non_location_urls_survive_alongside_match():
    urls = [
        "https://example.com/menu/some-brand-maitland-360-e-horatio-ave",
        "https://example.com/menu/desserts",
        "https://example.com/menu/other-town-99-main-st",
    ]
    kept = filter_location_urls(urls, MAITLAND_ADDR)
    assert kept == urls[:2]  # neutral /menu/desserts kept, other town dropped


def test_stateful_filter_anchors_across_batches():
    only_ours = LocationUrlFilter(MAITLAND_ADDR)
    # Batch 1 contains our page -> anchors the filter.
    batch1 = only_ours([
        "https://example.com/menu/brand-maitland-360-e-horatio-ave",
        "https://example.com/menu/brand-college-park-2429-edgewater-dr",
    ])
    assert batch1 == ["https://example.com/menu/brand-maitland-360-e-horatio-ave"]
    # Batch 2 (PDFs found later) has no matching URL of its own, but the
    # anchor from batch 1 still drops the other location's PDF.
    batch2 = only_ours([
        "https://example.com/pdfs/other-town-99-main-st-menu.pdf",
        "https://example.com/pdfs/dessert-menu.pdf",
    ])
    assert batch2 == ["https://example.com/pdfs/dessert-menu.pdf"]


def test_stateful_filter_never_anchored_keeps_everything():
    only_ours = LocationUrlFilter(MAITLAND_ADDR)
    urls = ["https://example.com/menu/other-town-99-main-st"]
    assert only_ours(urls) == urls


# The exact PDF set the scraper stored for Anejo Cocina Winter Park (id 89):
# per-city menu files with NO street parts to anchor on.
ANEJO_URLS = [
    "https://anejococinamexicana.com/wp-content/uploads/2026/05/Anejo-Daytona-Beach-Menus-2026.pdf",
    "https://anejococinamexicana.com/wp-content/uploads/2026/05/Anejo-Winter-Park-Menus-2026.pdf",
    "https://anejococinamexicana.com/wp-content/uploads/2026/05/Anejo-San-Marco-Menus-2026.pdf",
    "https://anejococinamexicana.com/wp-content/uploads/2026/05/Anejo-river-side-Menus-2026.pdf",
    "https://anejococinamexicana.com/wp-content/uploads/2026/05/Anejo-St-Johns-Menus-2026.pdf",
]


def test_sibling_variant_pdfs_keep_only_our_city():
    kept = filter_location_urls(
        ANEJO_URLS, "1035 N Orlando Ave #101, Winter Park, FL 32789, USA"
    )
    assert kept == [ANEJO_URLS[1]]


def test_sibling_variant_ignores_menu_section_siblings():
    # Sections of OUR location's menu differ by menu vocabulary — never
    # treated as other locations.
    urls = [
        "https://x.com/pdfs/Brand-Winter-Park-Dinner-Menu.pdf",
        "https://x.com/pdfs/Brand-Winter-Park-Dessert-Menu.pdf",
        "https://x.com/pdfs/Brand-Winter-Park-Happy-Hour-Menu.pdf",
    ]
    assert filter_location_urls(
        urls, "100 W Fairbanks Ave, Winter Park, FL 32789, USA"
    ) == urls


def test_sibling_variant_requires_exact_span_equality():
    # "cafe-in-winter-park" contains the city words but ISN'T the city span;
    # sibling categories must survive (The Glass Knife regression).
    urls = [
        "https://theglassknife.com/menu-category/cafe-in-winter-park/?location=park-avenue",
        "https://theglassknife.com/menu-category/cake/?location=park-avenue",
        "https://theglassknife.com/menu-category/evening/?location=park-avenue",
    ]
    assert filter_location_urls(
        urls, "276 S Orlando Ave, Winter Park, FL 32789, USA"
    ) == urls
    # Same for the record whose STREET is Park Ave.
    assert filter_location_urls(
        urls, "212 N Park Ave, Winter Park, FL 32789, USA"
    ) == urls


def test_sibling_variant_strips_synthetic_structured_marker():
    # Sixty Vines: the raleigh page's synthetic #structured-menu marker must
    # not shield it (its "menu" token would hit the vocabulary exemption).
    urls = [
        "https://sixtyvines.com/menu/winter-park",
        "https://sixtyvines.com/menu/raleigh-nc",
        "https://sixtyvines.com/menu/raleigh-nc#structured-menu",
        "https://sixtyvines.com/menu/miami",
    ]
    kept = filter_location_urls(urls, "110 S Orlando Ave #12, Winter Park, FL 32789, USA")
    assert kept == ["https://sixtyvines.com/menu/winter-park"]


def test_sibling_rule_uses_urls_from_earlier_batches():
    # Sixty Vines crawl shape: batch 1 (landing) contains our page; batch 2
    # (links found ON our page) lists every other city but not ours.
    only_ours = LocationUrlFilter("110 S Orlando Ave #12, Winter Park, FL 32789, USA")
    batch1 = only_ours([
        "https://sixtyvines.com/menu/winter-park",
        "https://sixtyvines.com/menu/nashville",
    ])
    assert batch1 == ["https://sixtyvines.com/menu/winter-park"]
    batch2 = only_ours([
        "https://sixtyvines.com/menu/charlotte-nc",
        "https://sixtyvines.com/menu/miami",
        "https://sixtyvines.com/private-dining",
    ])
    assert batch2 == ["https://sixtyvines.com/private-dining"]


def test_sibling_variant_generic_page_not_dropped():
    # A generic shared page (empty differing span on one side) is not a
    # location variant.
    urls = [
        "https://x.com/pdfs/Brand-Winter-Park-Menus.pdf",
        "https://x.com/pdfs/Brand-Menus.pdf",
    ]
    assert filter_location_urls(
        urls, "100 W Fairbanks Ave, Winter Park, FL 32789, USA"
    ) == urls


# ---- area_from_address (Admin coverage-by-area grouping) --------------------

def test_area_from_address_extracts_city():
    assert area_from_address("617 E Central Blvd, Orlando, FL 32801, USA") == "Orlando"
    assert area_from_address(MAITLAND_ADDR) == "Maitland"
    # Suite/unit segments in the street part must not shift the city.
    assert (
        area_from_address("433 W New England Ave A, Winter Park, FL 32789, USA")
        == "Winter Park"
    )
    # State without ZIP still anchors.
    assert area_from_address("12 Main St, Longwood, FL, USA") == "Longwood"


def test_area_from_address_handles_junk():
    assert area_from_address(None) == "Unknown"
    assert area_from_address("") == "Unknown"
    assert area_from_address("somewhere with no commas") == "Unknown"
