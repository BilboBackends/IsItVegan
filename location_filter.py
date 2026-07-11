"""Location-aware menu-URL filtering for multi-location restaurant sites.

A brand site often publishes one menu page per location with the address in
the URL slug (pizzabrunofl.com: /menu/pizza-bruno-curry-ford-3990-curry-ford-
road vs /menu/pizza-bruno-maitland-360-e-horatio-ave-suite-500). The scraper
knows exactly which location a record is — its Google Places address — so
when candidate menu URLs discriminate by location, only this record's pages
should be fetched. Otherwise one location stores every location's dishes
(the Pizza Bruno Maitland bug: 3 locations' menus concatenated into one
49k-char "menu").

Anchoring rule: filtering only activates when some candidate URL carries
STREET-level evidence of this record's address (street number, or street
name adjacent to a road suffix). City names are deliberately too weak to
anchor — they appear in generic slugs ("cafe-in-winter-park") and would
trigger false drops — but a city match still rescues a URL from being
dropped once the filter is anchored.

Safety valve: with no anchor, everything is kept and behavior is unchanged.
A site with one shared /menu page for all locations is already correct per
record, and a site whose slugs use neighborhood nicknames we can't match
must not be filtered down to nothing.

Known limitation: slugs that name locations WITHOUT address-like parts
("/menu/college-park" alone) never anchor and are never dropped; the
quality audit remains the net for those.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

# Suffixes that mark a token run as a street address inside a URL slug.
_STREET_SUFFIXES = {
    "st", "street", "ave", "avenue", "av", "rd", "road", "dr", "drive",
    "blvd", "boulevard", "hwy", "highway", "ln", "lane", "way", "pkwy",
    "parkway", "trl", "trail", "ct", "court", "cir", "circle", "pl",
    "place", "plaza", "ter", "terrace", "sq", "square",
}

# Words that never identify a specific street on their own (directions,
# suite markers, state/country noise) — excluded from street-name tokens.
_GENERIC_ADDRESS_WORDS = _STREET_SUFFIXES | {
    "n", "s", "e", "w", "ne", "nw", "se", "sw",
    "north", "south", "east", "west",
    "suite", "ste", "unit", "apt", "bldg", "building", "floor",
    "fl", "usa", "us",
}

# Path segments that introduce a per-location page ("/locations/<slug>",
# "?location=park-avenue"). NOT "store": delivery platforms route every
# restaurant under /store/<name> (ubereats, order.store) — treating that as
# location-specific would drop valid cross-domain menu sources.
_LOCATION_SEGMENTS = {"location", "locations", "branch"}

# Menu-section vocabulary: two sibling URLs differing only by one of these
# ("...-dinner-menu" vs "...-dessert-menu") are menu SECTIONS of one
# location, never two locations — exempt from the sibling-variant rule.
_MENU_SECTION_WORDS = {
    "menu", "menus", "food", "dinner", "lunch", "brunch", "breakfast",
    "dessert", "desserts", "drink", "drinks", "cocktail", "cocktails",
    "wine", "wines", "beer", "beers", "bar", "kids", "catering", "happy",
    "hour", "specials", "special", "daily", "weekend", "seasonal",
    "holiday", "evening", "late", "night", "takeout", "delivery",
    "appetizers", "apps", "starters", "entrees", "mains", "sides",
    "salads", "soups", "sandwiches", "burgers", "tacos", "pizza",
    "pizzas", "sushi", "snacks", "bites", "small", "plates", "bakery",
    "pastry", "pastries", "cake", "cakes", "coffee", "tea", "vegan",
    "vegetarian", "gluten", "restaurants", "cafe", "order", "online",
}


@dataclass
class AddressTokens:
    """This record's address, reduced to URL-matchable lowercase tokens."""

    street_number: str | None = None
    street_words: set[str] = field(default_factory=set)
    city_words: set[str] = field(default_factory=set)
    city_fused: str = ""  # "winter park" also appears fused: /winterpark/

    def __bool__(self) -> bool:
        return bool(self.street_number or self.street_words or self.city_words)


def address_tokens(address: str | None) -> AddressTokens:
    """Parse a Places formatted address ("360 E Horatio Ave #300, Maitland,
    FL 32751, USA") into matchable tokens."""
    if not address:
        return AddressTokens()
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if not parts:
        return AddressTokens()

    street_tokens = [t for t in re.split(r"[^0-9a-z]+", parts[0].lower()) if t]
    number = next((t for t in street_tokens if t.isdigit()), None)
    street_words = {
        t for t in street_tokens
        if not t.isdigit() and t not in _GENERIC_ADDRESS_WORDS
    }

    city_tokens: list[str] = []
    # The city is the last part before the "STATE zip" part (Places format).
    for part in parts[1:]:
        if re.fullmatch(r"[A-Z]{2}\s*\d{5}(-\d{4})?", part.strip()):
            break
        city_tokens = [t for t in re.split(r"[^0-9a-z]+", part.lower()) if t]
    return AddressTokens(
        street_number=number,
        street_words=street_words,
        city_words=set(city_tokens),
        city_fused="".join(city_tokens),
    )


def _url_tokens(url: str) -> list[str]:
    # "#structured-menu" is the scraper's own synthetic source marker, not
    # part of the site's URL — it must not differentiate sibling URLs (its
    # "menu" token would wrongly trip the menu-vocabulary exemption).
    parsed = urlparse(url.removesuffix("#structured-menu"))
    blob = unquote(f"{parsed.path} {parsed.query} {parsed.fragment}").lower()
    return [t for t in re.split(r"[^0-9a-z]+", blob) if t]


def strong_location_match(url: str, tokens: AddressTokens) -> bool:
    """Street-level proof the URL is THIS record's page — safe to anchor on.

    Either the street number appears as a token, or every street-name word
    appears with at least one of them directly ahead of a road suffix
    ("horatio-ave", "park-avenue"). The adjacency requirement stops a street
    named Park from matching a "college-park" neighborhood slug.
    """
    if not tokens:
        return False
    present = _url_tokens(url)
    present_set = set(present)
    if tokens.street_number and tokens.street_number in present_set:
        return True
    if tokens.street_words and tokens.street_words <= present_set:
        for i, tok in enumerate(present):
            if tok in tokens.street_words and any(
                t in _STREET_SUFFIXES for t in present[i + 1 : i + 3]
            ):
                return True
    return False


def matches_location(url: str, tokens: AddressTokens) -> bool:
    """True when the URL plausibly names THIS record's location (street- or
    city-level). Used to KEEP a URL; anchoring needs strong_location_match."""
    if strong_location_match(url, tokens):
        return True
    present = set(_url_tokens(url))
    if tokens.city_words and tokens.city_words <= present:
        return True
    return bool(tokens.city_fused and tokens.city_fused in present)


def looks_location_specific(url: str) -> bool:
    """True when the URL slug embeds a street address or a location route.

    "Address-like" = a street number (1–5 digits, no leading zero) with a
    street suffix within the next few tokens (3990-curry-ford-road). Date
    paths (/2026/05/), leading-zero segments, and random ids (item-12-wings,
    uuids) don't qualify.
    """
    tokens = _url_tokens(url)
    for i, tok in enumerate(tokens):
        if tok in _LOCATION_SEGMENTS and i + 1 < len(tokens):
            return True
        if not tok.isdigit() or len(tok) > 5 or tok.startswith("0"):
            continue
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if nxt.isdigit():  # /2026/05/ upload-date paths, not addresses
            continue
        if any(t in _STREET_SUFFIXES for t in tokens[i + 1 : i + 6]):
            return True
    return False


def _differing_span(a: list[str], b: list[str]) -> tuple[list[str], list[str]] | None:
    """When token lists differ by exactly one contiguous span, return
    (span_a, span_b); otherwise None (identical lists return None too)."""
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    j = 0
    while (
        j < len(a) - i and j < len(b) - i and a[len(a) - 1 - j] == b[len(b) - 1 - j]
    ):
        j += 1
    span_a, span_b = a[i : len(a) - j], b[i : len(b) - j]
    if not span_a and not span_b:
        return None
    return span_a, span_b


def _span_is_ours(span: list[str], tokens: AddressTokens) -> bool:
    """True when a variant span IS this record's location name — exact set
    equality, so a street named Park never claims "cafe-in-winter-park"."""
    s = set(span)
    if tokens.city_words and s == tokens.city_words:
        return True
    if tokens.city_fused and s == {tokens.city_fused}:
        return True
    if tokens.street_words and s == tokens.street_words:
        return True
    return bool(tokens.street_number and s == {tokens.street_number})


def _sibling_variant_drops(urls: list[str], tokens: AddressTokens) -> set[str]:
    """URLs that are another location's variant of one of OUR pages.

    Catches per-location files with no street parts in the name (Anejo:
    Anejo-Winter-Park-Menus.pdf vs Anejo-Daytona-Beach-Menus.pdf): same host,
    token lists differing by exactly one short span, where one side's span is
    exactly our city/street and the other side's isn't and isn't menu-section
    vocabulary (so dinner-vs-dessert siblings survive).
    """
    if not tokens or len(urls) < 2:
        return set()
    toks = {u: _url_tokens(u) for u in urls}
    hosts = {u: urlparse(u).netloc.lower() for u in urls}
    drops: set[str] = set()
    for ours in urls:
        if not _span_is_ours_somewhere(toks[ours], tokens):
            continue
        for other in urls:
            if other == ours or hosts[other] != hosts[ours]:
                continue
            span = _differing_span(toks[ours], toks[other])
            if span is None:
                continue
            span_ours, span_other = span
            if not span_ours or not span_other:
                continue  # one is a generic page, not a location variant
            if len(span_ours) > 4 or len(span_other) > 4:
                continue
            if not _span_is_ours(span_ours, tokens):
                continue
            if _span_is_ours(span_other, tokens):
                continue
            if set(span_other) & _MENU_SECTION_WORDS:
                continue
            drops.add(other)
    return drops


def _span_is_ours_somewhere(url_tokens: list[str], tokens: AddressTokens) -> bool:
    """Cheap pre-check: the URL contains our city/street tokens at all."""
    present = set(url_tokens)
    if tokens.city_words and tokens.city_words <= present:
        return True
    if tokens.city_fused and tokens.city_fused in present:
        return True
    if tokens.street_words and tokens.street_words <= present:
        return True
    return bool(tokens.street_number and tokens.street_number in present)


class LocationUrlFilter:
    """Stateful variant for a crawl that discovers URLs in several batches.

    The one-shot filter can only anchor within a single list; during a crawl
    the URL that proves "this site discriminates by location and this is our
    page" may arrive in an earlier batch (menu links) than the batch being
    filtered (PDFs, hop-2 links). Once ANY observed URL strongly matches the
    record's address, every later batch is filtered too.
    """

    _MAX_OBSERVED = 300

    def __init__(self, address: str | None):
        self._tokens = address_tokens(address)
        self._anchored = False
        self._observed: list[str] = []

    def __call__(self, urls: list[str]) -> list[str]:
        if not self._tokens or not urls:
            return urls
        # Sibling-variant drops need one of OUR pages for comparison — it may
        # have appeared in an EARLIER batch (Sixty Vines: hop-2 links from
        # the winter-park page list every other city but not winter-park
        # itself), so compare against everything observed so far.
        pool = list(dict.fromkeys(self._observed + urls))
        drops = _sibling_variant_drops(pool, self._tokens)
        kept = [u for u in urls if u not in drops]
        if not self._anchored and any(
            strong_location_match(u, self._tokens) for u in kept
        ):
            self._anchored = True
        if self._anchored:
            kept = [
                u for u in kept
                if matches_location(u, self._tokens)
                or not looks_location_specific(u)
            ]
        if len(self._observed) < self._MAX_OBSERVED:
            self._observed.extend(
                u for u in kept if u not in self._observed
            )
        return kept


def filter_location_urls(urls: list[str], address: str | None) -> list[str]:
    """Drop candidate URLs that belong to a DIFFERENT location of this brand.

    Only acts when at least one candidate carries street-level proof of this
    record's address — then location-specific candidates that don't match
    this record at all are other locations' pages and are dropped. With no
    anchor (shared menu, unmatchable slugs, no address on file) the list is
    returned unchanged.
    """
    if not urls or not address:
        return urls
    tokens = address_tokens(address)
    if not tokens:
        return urls
    drops = _sibling_variant_drops(urls, tokens)
    urls = [u for u in urls if u not in drops]
    if not any(strong_location_match(u, tokens) for u in urls):
        return urls
    return [
        u for u in urls
        if matches_location(u, tokens) or not looks_location_specific(u)
    ]


def area_from_address(address: str | None) -> str:
    """City segment of a Places formatted address, for area-level grouping.

    '617 E Central Blvd, Orlando, FL 32801, USA' -> 'Orlando'. Walks the
    comma segments and returns the one before the state (+ optional ZIP)
    segment, so suite/floor segments in the street part don't confuse it.
    Returns 'Unknown' when the shape doesn't match — never raises.
    """
    if not address:
        return "Unknown"
    parts = [part.strip() for part in address.split(",") if part.strip()]
    for i, part in enumerate(parts):
        if i > 0 and re.fullmatch(r"[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?", part):
            return parts[i - 1]
    return "Unknown"
