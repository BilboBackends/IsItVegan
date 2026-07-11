"""Website menu-text scraper (Phase 1 ingestion).

Fetches a restaurant's website and extracts readable text (the raw material
for Claude's Phase 3 dish classification). It does NOT parse dishes here —
menu HTML is too inconsistent for reliable heuristics, and Claude does that
job better downstream. We just get clean text out.

Coverage strategy: the actual menu often isn't on the landing page. So we
scrape the landing page, find links that look like a menu ("menu", "food",
"dinner", "order", ...), follow up to a handful of them one level deep (same
domain only), and combine the text. This turns many "too little text" landing
pages into real menu content.

Returns a ScrapeResult so callers can distinguish success from the many ways
a fetch can fail (timeout, 403, JS-only page, non-HTML) and log rather than
silently drop, per CLAUDE.md.

Escalation ladder (each step fires only when the cheaper one fails):
plain HTTP -> conventional /menu probe -> LLM link chooser -> PDF extraction
-> structured-data mining (JSON-LD / ordering-platform state) -> headless
browser (scroll banking, tab clicking, fragment navigation). Remaining
failures — hard bot walls, social-only "websites", image-only menus — are
flagged as photo-fallback candidates.
"""
from __future__ import annotations

import ast
import hashlib
import html as html_lib
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from headless import RenderedSession, is_available
from location_filter import LocationUrlFilter, filter_location_urls
from menu_score import MENU_THRESHOLD, score_menu_text
from structured_menu import extract_structured_menu_text

# A browser-ish UA; some sites 403 the default httpx agent.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 VeganFindBot/0.1"
)
_HTTP_HEADERS = {
    "User-Agent": _USER_AGENT,
    # Hostinger can 403 httpx when only User-Agent is set, even though it
    # serves the same page to ordinary browser requests.
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
}

# Tags whose text is never menu content.
_STRIP_TAGS = ["script", "style", "noscript", "svg", "head", "nav", "footer"]

# If combined extracted text is shorter than this, treat it as a failed scrape
# (usually a JS-only shell or a block page rather than real menu content).
_MIN_USEFUL_CHARS = 200

# Words that, in a link's text or href, suggest it points at a menu.
# Includes menu-section names: sites with JS/section-per-page menus label the
# links "Sandwiches" / "Salads" / "Soups", never "menu".
_MENU_HINTS = (
    "menu",
    "menus",
    "food",
    "dinner",
    "lunch",
    "breakfast",
    "brunch",
    "dine",
    "eat",
    "order",
    "our-food",
    "carte",
    # section names
    "appetizers",
    "starters",
    "entrees",
    "sandwiches",
    "salads",
    "soups",
    "sides",
    "bowls",
    "wraps",
    "paninis",
    "burgers",
    "pizza",
    "pizzas",
    "pasta",
    "tacos",
    "sushi",
    "desserts",
    "dessert",  # The Chapman's nav uses the singular
    "deserts",  # a common menu misspelling
    "happy hour",
    "happy-hour",
    "beverages",
    "drinks",
    "specials",
)

# Visible labels of menu-section TABS to click in the headless browser when a
# rendered menu page is sparse (tabbed widgets keep only the active section in
# the DOM). Deliberately excludes "menu"/"order" — those are nav links, and
# clicking them navigates away.
_TAB_WORDS = (
    "food", "drinks", "appetizers", "starters", "entrees", "entrées", "mains",
    "sandwiches", "salads", "soups", "sides", "bowls", "wraps", "paninis",
    "burgers", "pizza", "pizzas", "flatbreads", "pasta", "tacos", "sushi",
    "desserts", "deserts", "beverages", "specials", "happy hour",
    "lunch", "dinner", "breakfast", "brunch", "kids", "kids menu",
    # drink-mode sub-tabs
    "wine", "beer", "cocktails", "mocktails", "coffee",
)

# Words in a URL path that mark a page as ONE menu section — if such a page is
# all we found, the rest of the menu probably exists behind JS-rendered nav.
_SECTION_PATH_RE_WORDS = (
    "breakfast", "lunch", "dinner", "brunch", "sandwiches", "salads", "soups",
    "wraps", "desserts", "deserts", "burgers", "appetizers", "starters",
    "sides", "beverages", "drinks", "specials", "entrees", "entrées", "mains",
    "pasta", "kids", "family-style-meals",
)

# Daypart path segments probed around a captured menu page (siblings of a
# /brunch page, children of a bare menu page). Some sites (Sixty Vines)
# serve ONE daypart's content at the generic menu URL — via 302 or, for
# bot-ish user agents, directly with no redirect — and build the other
# daypart tabs client-side, so no HTML link ever reveals /dinner. Ordered
# by likelihood so the probe cap trims the rare ones first.
_DAYPART_SEGMENTS = (
    "dinner", "lunch", "brunch", "breakfast", "happy-hour", "wine",
    "dessert", "desserts", "drinks",
)
# Bounds the extra daypart requests per crawl.
_MAX_DAYPART_PROBES = 12

# Links we never follow even if they look menu-ish (socials / maps / forms).
# "gift" covers gift-card shops: squareup.com/gift/... ends in /order, which
# matched the order hint and got a GIFT CARD page stored as Sampaguita's menu
# (its $10/$25/$50 amounts then out-scored the real, price-less menu).
_SKIP_HINTS = (
    "facebook.", "instagram.", "twitter.", "yelp.", "tel:", "mailto:",
    "maps.app.goo.gl", "google.com/maps", "forms.gle", "youtube.", "tiktok.",
    "gift", "voucher", "donate", "merch", "careers", "hiring",
)

# Third-party menu/ordering hosts. If the menu link points here, the page is
# almost always JS-rendered — we note it so the failure is clearly a
# photo-fallback candidate rather than a mystery.
_THIRD_PARTY_HOSTS = (
    "toasttab.com",
    "toast.site",
    "square.site",
    "squareup.com",
    "clover.com",
    "cloveronline.com",
    "getsauce.com",
    "grubhub.com",
    "doordash.com",
    "ubereats.com",
    "chownow.com",
    "popmenu.com",
    "menufy.com",
    "slicelife.com",
    "activemenus.com",
    "mealkeyway.com",
    "viguest.com",
)

# Max menu-ish links to follow per page (bounds requests per restaurant).
# Section-per-page menus commonly have 6-8 sections.
_MAX_FOLLOW = 8

# Max total link fetches per site over HTTP (landing excluded). Bounds the
# two-hop expansion of menu index pages (/menu/ -> /menu/lunch, /menu/dinner).
_MAX_HTTP_FETCHES = 12
_MAX_VIGUEST_CATEGORY_RENDERS = 24

# Cap on combined kept text across pages — bounds classification cost.
_MAX_COMBINED_CHARS = 50_000

_STRUCTURED_MENU_MARKER_RE = re.compile(
    r"\[structured-menu products=(\d+) categories=(\d+)\]"
)

# Hosts that are social profiles, not restaurant websites. Google sometimes
# lists these as the "website"; there is no menu to scrape behind them.
_SOCIAL_HOSTS = (
    "instagram.com", "facebook.com", "linktr.ee", "tiktok.com",
    "twitter.com", "x.com", "youtube.com",
)


@dataclass
class ScrapeResult:
    url: str
    ok: bool
    text: str = ""
    error: str | None = None
    status_code: int | None = None
    char_count: int = 0
    # URLs fetched while searching for the menu (landing + followed links).
    scraped_urls: list[str] = field(default_factory=list)
    # The URL of the best-scoring page.
    menu_url: str | None = None
    # Every kept (url, text) page. Menus are often split across pages
    # (lunch/dinner/brunch/drinks) — all menu-like pages are kept, and `text`
    # is their combination. Empty on failure.
    pages: list[tuple[str, str]] = field(default_factory=list)
    # Third-party menu hosts we saw linked but couldn't scrape (JS-rendered).
    third_party_hosts: list[str] = field(default_factory=list)
    # Menu-likeness of the kept text (0..1) and whether it cleared threshold.
    menu_score: float = 0.0
    is_menu: bool = False
    # Successful transport, persisted by ingest as context for the next crawl.
    crawl_method: str | None = None  # http | headless | mock
    used_learned_context: bool = False
    content_hash: str | None = None
    # Structured client-state evidence is also a completeness signal: a page
    # whose URL names one section can still contain the entire menu payload.
    structured_item_count: int = 0
    structured_category_count: int = 0
    completeness_error: str | None = None
    # Bounded, secret-free candidate decision data for troubleshooting.
    diagnostics: list[dict] = field(default_factory=list)


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    # get_text with a separator keeps menu items on distinct lines; collapse
    # the runs of blank lines the markup leaves behind.
    raw = soup.get_text(separator="\n")
    lines = [line.strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def _pages_from_html(page_url: str, html: str) -> list[tuple[str, str]]:
    """One fetched page can yield TWO menu candidates: its visible DOM text,
    and — when the page embeds a structured menu (schema.org JSON-LD or
    ordering-platform state JSON) — that menu rendered as text.

    Ordering platforms routinely show a fraction of the menu (lazy sections,
    virtualized lists) while shipping ALL of it as data; the structured
    pseudo-page is how those menus stop being "1174 chars, 15 items".
    """
    pages = [(page_url, _extract_text(html))]
    try:
        structured = extract_structured_menu_text(html)
    except Exception:
        structured = None  # never let a weird page break the scrape
    if structured:
        pages.append((page_url.split("#")[0] + "#structured-menu", structured))
    return pages


@dataclass
class _Fetched:
    html: str | None = None
    pdf_bytes: bytes | None = None  # set when the response is a PDF
    error: str | None = None
    status_code: int | None = None
    # Where redirects landed; None when the request never resolved.
    final_url: str | None = None


_CERT_VERIFY_ERROR_RE = re.compile(
    r"certificate verify failed|CERTIFICATE_VERIFY_FAILED", re.I
)


def _is_cert_verify_error(exc: BaseException) -> bool:
    """Detect the cert failure shape httpx/httpcore exposes across versions."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _CERT_VERIFY_ERROR_RE.search(f"{type(current).__name__}: {current}"):
            return True
        current = current.__cause__ or current.__context__
    return False


def _fetch_without_cert_verification(
    url: str, timeout: float = 25.0
) -> httpx.Response:
    """Narrow fallback for sites with broken TLS chains.

    A few restaurant/menu CDNs serve valid-looking content with an incomplete
    certificate chain. We still try normal verification first; this fallback
    only runs for explicit certificate verification failures.
    """
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers=_HTTP_HEADERS,
        verify=False,
    ) as insecure_client:
        return insecure_client.get(url)


def _structured_counts(text: str) -> tuple[int, int]:
    matches = _STRUCTURED_MENU_MARKER_RE.findall(text or "")
    return (
        max((int(items) for items, _ in matches), default=0),
        max((int(categories) for _, categories in matches), default=0),
    )


def _fetch(client: httpx.Client, url: str) -> _Fetched:
    """Fetch one URL: HTML, PDF bytes, or a classified error (never raises).

    final_url records where redirects landed. Sites silently 302 a menu URL
    to one section (Sixty Vines: /menu/<loc> -> /menu/<loc>/brunch); pages
    recorded under the REQUESTED url hid that from the single-section guards
    and from location filtering.
    """
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        if not _is_cert_verify_error(exc):
            return _Fetched(error=f"{type(exc).__name__}: {exc}")
        try:
            resp = _fetch_without_cert_verification(url)
        except httpx.HTTPError as retry_exc:
            return _Fetched(error=f"{type(retry_exc).__name__}: {retry_exc}")
    final_url = str(getattr(resp, "url", "") or "") or None
    if resp.status_code >= 400:
        return _Fetched(
            status_code=resp.status_code,
            error=f"HTTP {resp.status_code}",
            final_url=final_url,
        )
    content_type = resp.headers.get("content-type", "").lower()
    if "pdf" in content_type:
        # Menus are often PDFs — keep the bytes so a follower can extract them.
        return _Fetched(
            pdf_bytes=resp.content, status_code=resp.status_code, final_url=final_url
        )
    if "html" not in content_type:
        return _Fetched(
            status_code=resp.status_code,
            error=f"Non-HTML content-type: {content_type or 'unknown'}",
            final_url=final_url,
        )
    return _Fetched(html=resp.text, status_code=resp.status_code, final_url=final_url)


def _fetched_to_text(page: "_Fetched") -> str:
    """Turn a fetched page into text — HTML via BeautifulSoup, PDF via pdf_menu."""
    if page.html is not None:
        return _extract_text(page.html)
    if page.pdf_bytes is not None:
        try:
            from pdf_menu import extract_pdf_menu_text
        except Exception:
            return ""
        return extract_pdf_menu_text(page.pdf_bytes)
    return ""


# PDF menus referenced from pages (not fetched AS pages). The URL often only
# exists inside a JS viewer's config (The Chapman renders its menu PDFs to
# canvas — the DOM has no text and no <a href>), so scan the whole markup.
_PDF_URL_ABS_RE = re.compile(
    r'https?://[^\s\'"<>()]+\.pdf(?:[?#][^\s\'"<>()]*)?', re.I
)
_PDF_ATTR_RE = re.compile(
    r'(?:href|src|data)\s*=\s*["\']([^"\']+\.pdf(?:[?#][^"\']*)?)["\']', re.I
)
_MAX_PDF_FETCHES = 6
_MAX_PDF_DOWNLOAD_BYTES = 16_000_000


def _find_pdf_urls(html: str, base_url: str) -> list[str]:
    """Every PDF referenced anywhere in a page — hrefs, embeds, viewer
    scripts. Skip-hint URLs (gift/press/careers) are excluded; junk PDFs
    (wine flights, press kits) are handled by normal scoring afterwards."""
    candidates = list(_PDF_URL_ABS_RE.findall(html))
    candidates += [urljoin(base_url, m) for m in _PDF_ATTR_RE.findall(html)]
    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = candidate.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        if any(skip in candidate.lower() for skip in _SKIP_HINTS):
            continue
        out.append(candidate)
    return out


def _is_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


_SERIALIZED_HREF_RE = re.compile(
    r'"href"\s*:\s*\[\s*0\s*,\s*"(?P<href>[^"]+)"\s*\]', re.I
)
_SERIALIZED_LABEL_RE = re.compile(
    r'"(?:content|label|name|text)"\s*:\s*\[\s*0\s*,\s*"(?P<label>[^"]+)"\s*\]',
    re.I,
)
_VIGUEST_CATEGORY_RE = re.compile(
    r"""["'](?P<path>(?:https?://[^"']+)?/Home/Merchandise\?[^"']+)["']""",
    re.I,
)
_VIGUEST_MERCH_RE = re.compile(r"renderMerchandise\((?P<args>.*?)\)\s*;", re.S)


def _decoded_markup(markup: str) -> str:
    return (
        html_lib.unescape(markup or "")
        .replace("\\/", "/")
        .replace("\\u0026", "&")
    )


def _find_serialized_menu_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Menu/order links hidden inside site-builder JSON blobs.

    Hostinger pages can store buttons as escaped data like
    `"href":[0,"https://..."], "content":[0,"OUR MENU"]` instead of normal
    anchors. BeautifulSoup cannot see those links, but the raw page can.
    """
    decoded = _decoded_markup(html)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _SERIALIZED_HREF_RE.finditer(decoded):
        href = match.group("href").strip()
        window = decoded[max(0, match.start() - 500): match.end() + 900]
        labels = " ".join(
            m.group("label") for m in _SERIALIZED_LABEL_RE.finditer(window)
        )
        if not labels:
            labels = href
        absolute = urljoin(base_url, href)
        norm = absolute.split("#")[0].rstrip("/")
        if norm in seen:
            continue
        seen.add(norm)
        out.append((labels, absolute))
    return out


def _find_viguest_category_urls(html: str, base_url: str) -> list[str]:
    """OnePOS/Viguest category routes embedded in the ordering landing page."""
    decoded = _decoded_markup(html)
    out: list[str] = []
    seen: set[str] = set()
    for match in _VIGUEST_CATEGORY_RE.finditer(decoded):
        url = urljoin(base_url, match.group("path"))
        if "sitename=" not in url.lower():
            continue
        norm = url.split("#")[0].rstrip("/")
        if norm in seen:
            continue
        seen.add(norm)
        out.append(url)
    return out[:_MAX_VIGUEST_CATEGORY_RENDERS]


def _split_js_args(source: str) -> list[str]:
    args: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in source:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if quote:
            buf.append(ch)
            if ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in {"'", '"'}:
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            args.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf or source.endswith(","):
        args.append("".join(buf).strip())
    return args


def _js_string(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        try:
            parsed = ast.literal_eval(value)
        except Exception:
            parsed = value[1:-1].replace(r"\"", '"').replace(r"\'", "'")
    else:
        parsed = value
    return html_lib.unescape(str(parsed)).strip()


def _js_price(value: str) -> str:
    try:
        amount = float(value.strip().strip('"\''))
    except ValueError:
        return ""
    if amount <= 0:
        return ""
    return f"${amount:.2f}"


def _extract_viguest_items(html: str) -> list[tuple[str, str, str]]:
    """Return (name, description, price) tuples from OnePOS render calls."""
    items: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for match in _VIGUEST_MERCH_RE.finditer(html):
        args = _split_js_args(match.group("args"))
        if len(args) < 5:
            continue
        name = _js_string(args[2])
        price = _js_price(args[3])
        description = _js_string(args[4])
        if not name or not price:
            continue
        key = (
            re.sub(r"\s+", " ", name).casefold(),
            re.sub(r"\s+", " ", description).casefold(),
            price,
        )
        if key in seen:
            continue
        seen.add(key)
        items.append((name, description, price))
    return items


def _viguest_category_name(url: str) -> str:
    values = parse_qs(urlparse(url).query).get("name")
    if values and values[0].strip():
        return values[0].strip()
    return "Menu"


def _compact_for_match(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _viguest_site_name(url: str) -> str:
    values = parse_qs(urlparse(url).query).get("siteName")
    return values[0] if values else ""


def _score_viguest_site_match(candidate_url: str, home_url: str) -> int:
    """Prefer location-specific Viguest links matching the restaurant site."""
    home = _compact_for_match(urlparse(home_url).netloc + urlparse(home_url).path)
    site = _compact_for_match(_viguest_site_name(candidate_url))
    if not home or not site:
        return 0
    score = 0
    for token in (
        "cape", "coral", "fort", "myers", "orlando", "winter", "park",
        "maitland", "colonial", "mills", "downtown",
    ):
        if token in home and token in site:
            score += 10
    # Generic overlap still helps when the siteName is only the restaurant
    # slug, but location tokens dominate when present.
    for size in range(min(len(home), len(site)), 3, -1):
        if any(site[i:i + size] in home for i in range(0, len(site) - size + 1)):
            score += size
            break
    return score


def _collect_viguest_http_pages(
    urls: list[str], timeout: float, home_url: str | None = None
) -> list[tuple[str, str]]:
    """Fetch Viguest/OnePOS menus without slow browser category rendering."""
    viguest_urls = [
        u for u in dict.fromkeys(urls)
        if "viguest.com" in urlparse(u).netloc.lower()
    ]
    if home_url and len(viguest_urls) > 1:
        scored = [
            (_score_viguest_site_match(candidate, home_url), candidate)
            for candidate in viguest_urls
        ]
        best = max((score for score, _ in scored), default=0)
        if best > 0:
            viguest_urls = [candidate for score, candidate in scored if score == best]
    pages: list[tuple[str, str]] = []
    for landing_url in viguest_urls[:2]:
        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                headers=_HTTP_HEADERS,
            ) as client:
                landing = client.get(landing_url)
                if landing.status_code >= 400:
                    continue
                categories = _find_viguest_category_urls(landing.text, landing_url)
                if not categories:
                    continue
                category_blocks: list[tuple[str, list[tuple[str, str, str]]]] = []
                for category_url in categories[:_MAX_VIGUEST_CATEGORY_RENDERS]:
                    try:
                        response = client.get(category_url)
                    except httpx.HTTPError:
                        continue
                    if response.status_code >= 400:
                        continue
                    items = _extract_viguest_items(response.text)
                    if items:
                        category_blocks.append(
                            (_viguest_category_name(category_url), items)
                        )
        except httpx.HTTPError:
            continue

        if not category_blocks:
            continue
        item_count = sum(len(items) for _, items in category_blocks)
        lines = [
            f"[structured-menu products={item_count} categories={len(category_blocks)}]"
        ]
        for category, items in category_blocks:
            lines.append(f"== {category} ==")
            for name, description, price in items:
                detail = f"{name}"
                if description:
                    detail += f" — {description}"
                detail += f" ({price})"
                lines.append(detail)
        pages.append((landing_url.rstrip("/") + "#viguest-menu", "\n".join(lines)))
    return pages


def _fetch_pdf_pages(pdf_urls: list[str]) -> list[tuple[str, str]]:
    """Download referenced menu PDFs and extract text as page candidates."""
    pages: list[tuple[str, str]] = []
    if not pdf_urls:
        return pages
    from pdf_menu import extract_pdf_menu_text

    with httpx.Client(
        timeout=25, follow_redirects=True, headers=_HTTP_HEADERS
    ) as client:
        for pdf_url in pdf_urls[:_MAX_PDF_FETCHES]:
            try:
                resp = client.get(pdf_url)
            except httpx.HTTPError as exc:
                if not _is_cert_verify_error(exc):
                    continue
                try:
                    resp = _fetch_without_cert_verification(pdf_url)
                except httpx.HTTPError:
                    continue
            if (
                resp.status_code != 200
                or len(resp.content) > _MAX_PDF_DOWNLOAD_BYTES
                or not resp.content[:5].startswith(b"%PDF")
            ):
                continue
            text = extract_pdf_menu_text(resp.content)
            if text.strip():
                pages.append((pdf_url, text))
    return pages


def _looks_menu_like(text: str, href: str) -> bool:
    blob = f"{text} {href}".lower()
    if any(skip in blob for skip in _SKIP_HINTS):
        return False
    # Word-boundary match, not substring — "eat" must not match "create".
    return any(
        re.search(rf"(?<![a-z]){re.escape(hint)}(?![a-z])", blob)
        for hint in _MENU_HINTS
    )


def _is_single_section_url(url: str) -> bool:
    """True when a URL path names one menu section (e.g. /breakfast).

    If the only menu page found is a single section, the other sections are
    probably behind JS-rendered navigation the plain-HTTP pass can't see —
    a signal to escalate to the headless browser rather than stop early.
    """
    path = urlparse(url).path.lower()
    return any(
        re.search(rf"(?<![a-z]){w}(?![a-z])", path) for w in _SECTION_PATH_RE_WORDS
    )


def _norm_url(u: str) -> str:
    """Dedup key for fetched URLs: fragmentless, no trailing slash, and
    www-insensitive — a redirect from sixtyvines.com to www.sixtyvines.com is
    the same page."""
    base = u.split("#")[0].rstrip("/")
    return re.sub(r"^(https?)://www\.", r"\1://", base, flags=re.I)


def _daypart_segment(url: str) -> str | None:
    """The trailing path segment when it names a daypart (/menu/x/brunch)."""
    segments = [s for s in urlparse(url).path.split("/") if s]
    if segments and segments[-1].lower() in _DAYPART_SEGMENTS:
        return segments[-1].lower()
    return None


def _probe_daypart_pages(
    client: httpx.Client,
    pages: list[tuple[str, str]],
    seen: set[str],
    only_ours=None,
) -> list[tuple[str, str]]:
    """Fetch unlinked daypart pages around already-captured menu pages.

    Sites with client-side daypart tabs expose NOTHING crawlable: Sixty
    Vines serves the brunch menu at /menu/<loc> (302 for browsers, no
    redirect at all for bot-ish user agents) and dinner/lunch exist only at
    URLs no HTML links to. Two probe directions:

    - siblings: a captured page ends in a daypart (/brunch) -> try the other
      dayparts at the same path level;
    - children: no daypart page held at all, but a captured page's path
      looks like a menu -> try /dinner, /lunch, ... beneath it.

    Junk probes are harmless: soft-404 shells and duplicate content are
    dropped by _finish's scoring and containment dedup. `seen` is shared
    with the caller so nothing is fetched twice.
    """
    daypart_bases: list[str] = []
    menuish_bases: list[str] = []
    for page_url, _ in pages:
        base = page_url.split("#")[0].rstrip("/")
        if _daypart_segment(base):
            if base not in daypart_bases:
                daypart_bases.append(base)
        elif "menu" in urlparse(base).path.lower() and base not in menuish_bases:
            menuish_bases.append(base)

    candidates: list[str] = []
    for base in daypart_bases:
        parent = base.rsplit("/", 1)[0]
        for word in _DAYPART_SEGMENTS:
            sibling = f"{parent}/{word}"
            if sibling != base and sibling not in candidates:
                candidates.append(sibling)
    if not daypart_bases:
        for base in menuish_bases:
            for word in _DAYPART_SEGMENTS:
                child = f"{base}/{word}"
                if child not in candidates:
                    candidates.append(child)
    if only_ours is not None:
        candidates = only_ours(candidates)

    found: list[tuple[str, str]] = []
    probes = 0
    for candidate in candidates:
        if probes >= _MAX_DAYPART_PROBES:
            break
        if _norm_url(candidate) in seen:
            continue
        seen.add(_norm_url(candidate))
        probes += 1
        page = _fetch(client, candidate)
        if page.html is None:
            continue
        final = page.final_url or candidate
        if _norm_url(final) != _norm_url(candidate):
            # Redirected away — a soft-404 bouncing back to a page we
            # already hold (or its home) is not a new daypart.
            if _norm_url(final) in seen:
                continue
            seen.add(_norm_url(final))
        found.extend(_pages_from_html(final, page.html))
    return found


def _find_menu_links(
    html: str, base_url: str
) -> tuple[list[str], list[str], list[str]]:
    """Return (same-domain menu URLs, third-party hosts seen, third-party URLs).

    Extracted from the full page (nav included) BEFORE _extract_text strips
    nav — menu links very often live in the header/nav. Third-party ordering
    URLs (Toast/Clover/Sauce...) are returned so the headless path can render
    them — with a real-Chrome launch they get past the bot walls and the menu
    is in the DOM.
    """
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc.lower()

    follow: list[str] = []
    third_party: list[str] = []
    third_party_urls: list[str] = []
    seen: set[str] = set()

    def _remember_candidate(label: str, href: str) -> None:
        href = href.strip()
        if not href or href.startswith("#"):
            return
        if not _looks_menu_like(label, href):
            return

        absolute = urljoin(base_url, href)
        host = urlparse(absolute).netloc.lower()

        # Any cross-domain menu-looking link is a candidate for headless
        # following — ordering platforms are endless (Toast, Clover, Sauce,
        # MealKeyway, ...) so we don't gate on a known-host list. Known hosts
        # are still named in third_party for failure messages.
        base_bare = base_host.replace("www.", "")
        if host.replace("www.", "") != base_bare:
            # PDFs are downloaded/extracted by the PDF path. Sending them to
            # headless just opens a browser PDF viewer with little useful text.
            if _is_pdf_url(absolute):
                return
            tp = next((h for h in _THIRD_PARTY_HOSTS if h in host), None)
            if tp and tp not in third_party:
                third_party.append(tp)
            # Keep the SPA fragment (#/main routes matter on ordering apps);
            # dedupe on the fragment-less form.
            norm_tp = absolute.split("#")[0].rstrip("/")
            if norm_tp not in {u.split("#")[0].rstrip("/") for u in third_party_urls}:
                third_party_urls.append(absolute)
            return

        # Same-domain: dedup, and don't refetch the landing page itself.
        norm = absolute.split("#")[0].rstrip("/")
        if norm in seen or norm == base_url.split("#")[0].rstrip("/"):
            return
        seen.add(norm)
        follow.append(absolute)

    for a in soup.find_all("a", href=True):
        _remember_candidate(a.get_text(" ", strip=True), a["href"])

    for label, href in _find_serialized_menu_links(html, base_url):
        _remember_candidate(label, href)

    return follow[:_MAX_FOLLOW], third_party, third_party_urls


def _all_internal_links(html: str, base_url: str) -> list[dict]:
    """Every same-domain link as {text, url}, deduped — candidates for the LLM.

    Used when keyword matching finds no menu link: hand the whole set to a cheap
    model to pick the menu (catches labels like "Bill of Fare", "See our food").
    """
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc.lower().replace("www.", "")
    landing = base_url.split("#")[0].rstrip("/")

    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(base_url, href)
        host = urlparse(absolute).netloc.lower().replace("www.", "")
        if host != base_host:
            continue
        norm = absolute.split("#")[0].rstrip("/")
        if norm in seen or norm == landing:
            continue
        seen.add(norm)
        out.append({"text": a.get_text(" ", strip=True)[:80], "url": absolute})
    return out


def _llm_menu_link(html: str, base_url: str) -> str | None:
    """Ask the cheap LLM navigator to pick a menu link from all page links.

    Returns a same-domain URL or None. No key / SDK error -> None (caller falls
    back). Imported lazily so the scraper works without the anthropic SDK.
    """
    candidates = _all_internal_links(html, base_url)
    if not candidates:
        return None
    try:
        from llm_nav import choose_menu_link_from_text
    except Exception:
        return None
    choice = choose_menu_link_from_text(candidates)
    return choice.url


def _collect_http(
    url: str, timeout: float, address: str | None = None
) -> tuple[list[tuple[str, str]] | None, list[str], list[str], _Fetched]:
    """Collect (page_url, text) via plain HTTP: landing + followed menu links.

    Menu links are followed two hops deep: many sites use a menu index page
    (/menu/) that only links out to per-section pages (/menu/lunch,
    /menu/dinner), so a single hop captured just one section. Total fetches
    are capped by _MAX_HTTP_FETCHES.

    Returns (pages | None if landing failed, third_party_hosts,
    third_party_menu_urls, landing_meta). Third-party menu URLs are collected
    but NOT fetched here — they're JS-rendered ordering apps; the caller uses
    their presence to decide whether to escalate to headless.
    """
    # Multi-location brand sites publish one menu page per location; keep
    # only THIS record's pages (see location_filter.py). One shared filter
    # across every discovery batch: a match found among the first menu links
    # also anchors filtering of later-found PDF/hop-2/ordering URLs.
    only_ours = LocationUrlFilter(address)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers=_HTTP_HEADERS,
    ) as client:
        landing = _fetch(client, url)
        if landing.html is None:
            return None, [], [], landing
        # Resolve relative links against where redirects LANDED, and record
        # pages under that URL — the requested URL can hide a section-page
        # redirect from the single-section guards and location filtering.
        landing_url = landing.final_url or url

        menu_links, third_party, tp_urls = _find_menu_links(landing.html, landing_url)
        menu_links = only_ours(menu_links)
        tp_urls = only_ours(tp_urls)
        # If keyword matching found no menu link, let the cheap LLM pick one
        # from all page links (catches non-obvious labels).
        if not menu_links:
            llm_link = _llm_menu_link(landing.html, landing_url)
            if llm_link:
                menu_links = [llm_link]

        pages: list[tuple[str, str]] = _pages_from_html(landing_url, landing.html)
        pdf_urls = _find_pdf_urls(landing.html, landing_url)
        queue: list[tuple[str, int]] = [(lk, 1) for lk in menu_links]
        seen = {_norm_url(url), _norm_url(landing_url)}
        seen |= {_norm_url(lk) for lk in menu_links}
        # "/menu" is a near-universal convention — probe it even when the
        # landing never links to it. JS-built navs hide links from static
        # HTML entirely (F&D Cantina's Popmenu site: the landing shows only
        # order/catering links, while /menu carries the FULL menu as
        # JSON-LD). Costs one request; a 404 is simply skipped.
        conventional = urljoin(landing_url, "/menu")
        if _norm_url(conventional) not in seen:
            seen.add(_norm_url(conventional))
            queue.append((conventional, 1))
        fetches = 0
        while queue and fetches < _MAX_HTTP_FETCHES:
            link, hop = queue.pop(0)
            page = _fetch(client, link)
            fetches += 1
            page_url = page.final_url or link
            if _norm_url(page_url) != _norm_url(link):
                if _norm_url(page_url) in seen:
                    continue  # redirect landed on a page we already hold
                seen.add(_norm_url(page_url))
            if page.html is not None:
                pages.extend(_pages_from_html(page_url, page.html))
            else:
                text = _fetched_to_text(page)  # PDFs
                if text:
                    pages.append((page_url, text))
            if page.html is not None:
                for pdf_url in _find_pdf_urls(page.html, page_url):
                    if pdf_url not in pdf_urls:
                        pdf_urls.append(pdf_url)
            if hop == 1 and page.html is not None:
                more_links, more_tp, more_tp_urls = _find_menu_links(
                    page.html, page_url
                )
                more_links = only_ours(more_links)
                more_tp_urls = only_ours(more_tp_urls)
                for host in more_tp:
                    if host not in third_party:
                        third_party.append(host)
                for tp_url in more_tp_urls:
                    if _norm_url(tp_url) not in {_norm_url(u) for u in tp_urls}:
                        tp_urls.append(tp_url)
                for nxt in more_links:
                    if _norm_url(nxt) not in seen:
                        seen.add(_norm_url(nxt))
                        queue.append((nxt, 2))

        # JS-only daypart tabs: probe unlinked daypart URLs around the
        # captured menu pages (Sixty Vines' dinner/lunch menus).
        pages.extend(_probe_daypart_pages(client, pages, seen, only_ours))
    # Ordering platforms like Viguest/OnePOS can expose a full structured menu
    # after one cookie-setting landing request. If that succeeds, don't wait
    # on slow off-domain PDFs for the same restaurant.
    viguest_pages = _collect_viguest_http_pages(tp_urls, timeout, home_url=url)
    pages.extend(viguest_pages)
    viguest_has_menu = any(
        score_menu_text(text).is_menu and score_menu_text(text).price_count >= 8
        for _, text in viguest_pages
    )
    # Referenced PDF menus (not fetched as pages above): download + extract.
    if not viguest_has_menu:
        pages.extend(_fetch_pdf_pages(only_ours(pdf_urls)))
    return pages, third_party, tp_urls, landing


def _collect_headless(
    url: str,
    seed_urls: list[str] | None = None,
    address: str | None = None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Collect (page_url, text) via headless browser: landing + menu links.

    Renders the landing page (running its JS), follows same-domain menu links
    by rendering those too. Used only as a fallback when HTTP finds no menu.
    """
    # tab_words on the landing too: for single-page ordering sites (Square
    # Online et al.) the landing IS the menu, with category tabs/fragments.
    only_ours = LocationUrlFilter(address)
    try:
        # One context per restaurant is essential for chain menus: the landing
        # page often selects a location via cookies/storage that followed menu
        # pages require. Different restaurants still receive isolated sessions.
        with RenderedSession() as session:
            landing_html, _ = session.fetch(url, tab_words=_TAB_WORDS)
            if landing_html is None:
                return [], []

            menu_links, third_party, tp_urls = _find_menu_links(landing_html, url)
            menu_links = only_ours(menu_links)
            tp_urls = only_ours(tp_urls)
            if not menu_links and not tp_urls:
                llm_link = _llm_menu_link(landing_html, url)
                if llm_link:
                    menu_links = [llm_link]

            def _norm(u: str) -> str:
                return u.split("#")[0].rstrip("/")

            max_renders = 9
            pages: list[tuple[str, str]] = _pages_from_html(url, landing_html)
            pdf_urls = _find_pdf_urls(landing_html, url)
            # Some location pages populate the complete catalog in browser
            # storage immediately. Once validated, extra category renders add
            # latency and duplicates but no coverage.
            structured_counts = [
                _structured_counts(candidate_text) for _, candidate_text in pages
            ]
            if any(items >= 8 and categories >= 2 for items, categories in structured_counts):
                return pages, third_party
            seeded = only_ours([u for u in (seed_urls or []) if not _is_pdf_url(u)])
            queue: list[tuple[str, int]] = [
                (lk, 1) for lk in menu_links + tp_urls[:2] + seeded
            ]
            seen = {_norm(url)} | {_norm(lk) for lk, _ in queue}
            renders = 0

            while queue and renders < max_renders:
                link, hop = queue.pop(0)
                page_html, _ = session.fetch(
                    link, settle_ms=6_000, tab_words=_TAB_WORDS
                )
                renders += 1
                if page_html is None:
                    continue
                # PDF menus are often only referenced from the RENDERED DOM
                # (a JS viewer painting to canvas) — collect them here.
                for pdf_url in _find_pdf_urls(page_html, link):
                    if pdf_url not in pdf_urls:
                        pdf_urls.append(pdf_url)
                page_candidates = _pages_from_html(link, page_html)
                pages.extend(page_candidates)
                viguest_categories = _find_viguest_category_urls(page_html, link)
                if viguest_categories:
                    max_renders = max(
                        max_renders,
                        renders + min(
                            len(viguest_categories),
                            _MAX_VIGUEST_CATEGORY_RENDERS,
                        ),
                    )
                    for nxt in viguest_categories:
                        if _norm(nxt) not in seen:
                            seen.add(_norm(nxt))
                            queue.append((nxt, 2))
                best_here = max(
                    score_menu_text(candidate_text).score
                    for _, candidate_text in page_candidates
                )
                if hop == 1 and best_here < 0.75:
                    more_menu, _tp, more_tp_urls = _find_menu_links(page_html, link)
                    candidates = sorted(
                        only_ours(more_menu + more_tp_urls),
                        key=lambda candidate: 0 if "menu" in candidate.lower() else 1,
                    )
                    for nxt in candidates[:2]:
                        if _norm(nxt) not in seen:
                            seen.add(_norm(nxt))
                            queue.append((nxt, 2))
            pages.extend(_fetch_pdf_pages(only_ours(pdf_urls)))
            return pages, third_party
    except Exception:
        return [], []


def _profile_urls(crawl_context: dict | None) -> list[str]:
    if not crawl_context:
        return []
    values = crawl_context.get("menu_urls")
    if not isinstance(values, list):
        return []
    return list(
        dict.fromkeys(
            value.strip()
            for value in values
            if isinstance(value, str) and value.strip()
        )
    )


def _collect_known_http(
    urls: list[str], timeout: float, address: str | None = None
) -> list[tuple[str, str]]:
    """Fetch previously validated menu pages directly, skipping discovery."""
    pages: list[tuple[str, str]] = []
    seen: set[str] = set()
    only_ours = LocationUrlFilter(address)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers=_HTTP_HEADERS,
    ) as client:
        for saved_url in urls[:_MAX_HTTP_FETCHES]:
            # #structured-menu is our synthetic source marker, not a real
            # server route. Fetching its base recreates both DOM and embedded
            # structured candidates.
            fetch_url = saved_url.removesuffix("#structured-menu")
            if _norm_url(fetch_url) in seen:
                continue
            seen.add(_norm_url(fetch_url))
            page = _fetch(client, fetch_url)
            page_url = page.final_url or fetch_url
            if _norm_url(page_url) != _norm_url(fetch_url):
                if _norm_url(page_url) in seen:
                    continue
                seen.add(_norm_url(page_url))
            if page.html is not None:
                pages.extend(_pages_from_html(page_url, page.html))
            else:
                text = _fetched_to_text(page)
                if text:
                    pages.append((page_url, text))
        # Learned routes from before daypart probing can be a lone daypart
        # page (hidden behind a redirect or a bot-UA default) — probing here
        # self-heals the profile without waiting for full rediscovery.
        pages.extend(_probe_daypart_pages(client, pages, seen, only_ours))
    return pages


def _collect_known_headless(home_url: str, urls: list[str]) -> list[tuple[str, str]]:
    """Render learned pages after priming their location-sensitive home page."""
    pages: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        with RenderedSession() as session:
            # Establish location/cookie state. When the home/location URL is
            # itself the learned source, reuse this render instead of fetching
            # the identical page twice.
            home_html, _ = session.fetch(home_url, tab_words=())
            home_normalized = home_url.removesuffix("#structured-menu").rstrip("/")
            learned_normalized = {
                saved.removesuffix("#structured-menu").rstrip("/") for saved in urls
            }
            if home_html is not None and home_normalized in learned_normalized:
                pages.extend(_pages_from_html(home_url, home_html))
                seen.add(home_normalized)
            pdf_urls: list[str] = []
            for saved_url in urls[:9]:
                fetch_url = saved_url.removesuffix("#structured-menu")
                normalized = fetch_url.rstrip("/")
                if normalized in seen:
                    continue
                seen.add(normalized)
                # Learned PDF sources: rendering a PDF in the browser paints
                # a viewer, not text — download and extract instead.
                if _is_pdf_url(fetch_url):
                    pdf_urls.append(fetch_url)
                    continue
                html, _ = session.fetch(
                    fetch_url, settle_ms=6_000, tab_words=_TAB_WORDS
                )
                if html is not None:
                    pages.extend(_pages_from_html(fetch_url, html))
    except Exception:
        return []
    pages.extend(_fetch_pdf_pages(pdf_urls))
    return pages


def _try_learned_context(
    home_url: str,
    crawl_context: dict | None,
    *,
    timeout: float,
    use_headless: bool,
    address: str | None = None,
) -> ScrapeResult | None:
    """Try the last successful route; return None when rediscovery is needed."""
    urls = _profile_urls(crawl_context)
    # Profiles learned before location filtering existed can carry OTHER
    # locations' menu pages (the Pizza Bruno bug) — a high score would then
    # re-walk the contaminated route forever. Filtering here self-heals the
    # profile: only this record's pages are fetched, the content hash
    # changes, and the cleaned URL list is what gets re-stored on success.
    urls = filter_location_urls(urls, address)
    method = (crawl_context or {}).get("crawl_method")
    if not urls or method not in {"http", "headless"}:
        return None
    # Invalidate old weak single-section routes (the Olive Garden /specials
    # failure). A genuinely complete structured capture is much larger and
    # will survive this check on its next learned run.
    if (
        len(urls) == 1
        and _is_single_section_url(urls[0])
        and (
            float((crawl_context or {}).get("menu_score") or 0) < 0.75
            or int((crawl_context or {}).get("char_count") or 0) < 5_000
        )
    ):
        return None
    # A route whose best-ever capture was mediocre isn't worth re-walking —
    # rediscover instead (The Chapman: /menu index blurbs at 0.68 while the
    # real menus were PDFs behind each section page). Unknown score (older
    # profiles) still gets the learned attempt; the post-fetch checks apply.
    known_score = (crawl_context or {}).get("menu_score")
    learned_pdf_route = all(_is_pdf_url(u) for u in urls)
    if (
        known_score is not None
        and float(known_score) < 0.75
        and not (
            learned_pdf_route
            and float(known_score) >= MENU_THRESHOLD
            and int((crawl_context or {}).get("char_count") or 0) >= _MIN_USEFUL_CHARS
        )
    ):
        return None
    if method == "headless":
        if not use_headless or not is_available():
            return None
        pages = _collect_known_headless(home_url, urls)
    else:
        pages = _collect_known_http(urls, timeout, address)
    if not pages:
        return None
    result = _finish(
        home_url,
        pages,
        [],
        status_code=200,
        crawl_method=method,
        used_learned_context=True,
    )
    result = _validate_completeness(result)
    if not result.ok:
        return None
    # A learned route that only ever captured a mediocre menu must not lock
    # out rediscovery forever (The Chapman: the /menu index blurbs scored
    # 0.49 while the real menus were PDFs behind each section page).
    if result.menu_score < 0.6:
        return None
    # A JS widget can render only its first category and still look menu-like.
    # Treat a major size regression as a stale route and run full discovery;
    # if the menu genuinely shrank, discovery will find the same smaller copy
    # and the new profile will then replace the old one.
    previous_chars = (crawl_context or {}).get("char_count")
    if (
        isinstance(previous_chars, int)
        and previous_chars >= 1_200
        and result.char_count < previous_chars * 0.55
    ):
        return None
    return result


def _validate_completeness(result: ScrapeResult) -> ScrapeResult:
    """Reject partial captures uniformly across every transport/route."""
    if not result.ok:
        return result
    # A validated multi-category structured payload is stronger evidence than
    # the URL path (many SPAs expose their entire catalog at /menu/entrees).
    if result.structured_item_count >= 8 and result.structured_category_count >= 2:
        return result

    # Fragment-insensitive: a page and its #structured-menu pseudo-page are
    # the same capture, not evidence of a second menu section.
    kept_urls = list(dict.fromkeys(url.split("#")[0] for url, _ in result.pages))
    issue = None
    if len(kept_urls) == 1 and _is_single_section_url(kept_urls[0]):
        issue = f"only one menu section captured ({kept_urls[0]})"
    else:
        lowered = result.text.lower()
        order_markers = (
            "add to cart", "checkout", "view cart", "order online",
            "minimum order", "items in cart",
        )
        marker_count = sum(marker in lowered for marker in order_markers)
        price_count = score_menu_text(result.text).price_count
        if price_count < 8 and marker_count >= 2:
            issue = (
                "partially captured ordering page "
                f"({price_count} prices alongside order/cart chrome)"
            )

    if issue is None:
        return result
    result.ok = False
    result.is_menu = False
    result.completeness_error = issue
    result.error = f"Incomplete menu: {issue}. Existing validated menu preserved."
    result.diagnostics.append(
        {"stage": "completeness", "decision": "reject", "reason": issue}
    )
    return result


def _filter_pages_by_location(
    pages: list[tuple[str, str]], address: str | None
) -> list[tuple[str, str]]:
    """Post-crawl sweep: drop pages fetched for OTHER locations of the brand.

    In-crawl filtering is batch-by-batch and can miss — the URL proving which
    page is ours may sit in a different batch than the foreign ones (Sixty
    Vines: hop-2 links from the winter-park page list every other city but
    not winter-park itself). Here the complete URL set is known, so the
    one-shot filter is authoritative before scoring/storage.
    """
    if not pages or not address:
        return pages
    urls = list(dict.fromkeys(page_url for page_url, _ in pages))
    kept = set(filter_location_urls(urls, address))
    return [(page_url, text) for page_url, text in pages if page_url in kept]


def scrape_menu_text(
    url: str,
    *,
    timeout: float = 20.0,
    use_headless: bool = True,
    mock_html: str | None = None,
    crawl_context: dict | None = None,
    address: str | None = None,
) -> ScrapeResult:
    """Scrape a restaurant site for menu text, following menu links one level.

    Fast path: plain HTTP (landing + followed menu links), scored to keep the
    best page. If that doesn't yield a real menu and use_headless is set, retry
    the whole thing in a headless browser so JS-rendered menus (Toast/Square/
    Clover and modern SPA sites) actually render before extraction.

    address (the record's Google Places address) enables location filtering
    on multi-location brand sites: when the site publishes per-location menu
    pages, only THIS location's pages are kept (see location_filter.py).

    Pass mock_html to skip the network entirely (no link-following, no headless).
    """
    if mock_html is not None:
        return _validate_completeness(_finish(
            url,
            [(url, _extract_text(mock_html))],
            [],
            status_code=None,
            crawl_method="mock",
        ))

    # Google sometimes lists a social profile as the "website". There is no
    # menu behind these (and their text scores menu-ish enough to slip
    # through) — fail fast and clearly instead of storing feed noise.
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if any(host == s or host.endswith("." + s) for s in _SOCIAL_HOSTS):
        return ScrapeResult(
            url=url,
            ok=False,
            error=f"Website is a social profile ({host}) — no menu to scrape. "
            "Photo fallback candidate.",
        )

    learned = _try_learned_context(
        url,
        crawl_context,
        timeout=timeout,
        use_headless=use_headless,
        address=address,
    )
    if learned is not None:
        return learned

    pages, third_party, tp_urls, landing = _collect_http(url, timeout, address)

    # If the landing page itself was fetchable, try scoring the HTTP result.
    http_result = None
    if pages is not None:
        pages = _filter_pages_by_location(pages, address)
        http_result = _finish(
            url,
            pages,
            third_party,
            status_code=landing.status_code,
            crawl_method="http",
        )
        http_result = _validate_completeness(http_result)
        # A merely-adequate score with third-party ordering links around is
        # suspicious (often homepage/marketing copy scoring on section names,
        # not the menu) — still try headless, which renders the ordering
        # links. ANY cross-domain menu link counts, not just known platforms:
        # ordering hosts are endless (activemenus, MealKeyway, ...).
        # A tiny "menu" isn't trusted either, whatever it scored — a real
        # menu is rarely under ~1200 chars; it's usually a teaser with the
        # full menu behind JS.
        # A sub-0.75 score is usually an index/marketing page standing in
        # for the real menu (The Chapman: section blurbs at 0.68 while the
        # actual menus were PDFs only visible in the RENDERED section
        # pages). Whatever HTTP found below that bar, headless gets a shot
        # and the better capture wins.
        confident = (
            http_result.ok
            and http_result.menu_score >= 0.75
            and http_result.char_count >= 1200
        )
        # A single kept page whose URL names one menu section (/breakfast) is
        # probably a partial menu with the other sections behind JS-rendered
        # nav — escalate to headless to find them. Fragment-insensitive: the
        # #structured-menu pseudo-page doesn't make a section page two pages.
        kept_page_urls = {p_url.split("#")[0] for p_url, _ in http_result.pages}
        if (
            confident
            and len(kept_page_urls) == 1
            and _is_single_section_url(next(iter(kept_page_urls)))
        ):
            confident = False
        if confident or not use_headless or not is_available():
            return http_result

    # Fast path failed to find a real menu (or landing was JS-blocked). Escalate
    # to headless and keep whichever attempt scores as a menu / scores higher.
    if not is_available():
        return http_result or ScrapeResult(
            url=url, ok=False, status_code=landing.status_code, error=landing.error
        )

    hl_pages, hl_third_party = _collect_headless(
        url, seed_urls=tp_urls, address=address
    )
    hl_pages = _filter_pages_by_location(hl_pages, address)
    if not hl_pages:
        # Headless couldn't render anything; return the better failure we have.
        return http_result or ScrapeResult(
            url=url,
            ok=False,
            status_code=landing.status_code,
            error=landing.error or "Headless render produced no content",
        )

    headless_result = _finish(
        url,
        hl_pages,
        hl_third_party or third_party,
        status_code=200,
        crawl_method="headless",
    )
    headless_result = _validate_completeness(headless_result)
    # Prefer a real menu; when both attempts found one, keep whichever
    # captured more of it (headless often finds sections HTTP can't see,
    # but HTTP sometimes reaches PDF menus headless doesn't).
    if headless_result.ok and http_result and http_result.ok:
        return (
            headless_result
            if headless_result.char_count > http_result.char_count
            else http_result
        )
    if headless_result.ok:
        return headless_result
    if http_result and http_result.menu_score >= headless_result.menu_score:
        return http_result
    return headless_result


def _finish(
    url: str,
    pages: list[tuple[str, str]],
    third_party_hosts: list[str],
    status_code: int | None,
    crawl_method: str = "http",
    used_learned_context: bool = False,
) -> ScrapeResult:
    """Score every fetched page, keep ALL menu-like ones, decide ok/fail.

    A scrape only succeeds if the best page both has enough text AND clears the
    menu threshold — so homepage marketing copy no longer counts as a menu.
    Menus are frequently split across pages (breakfast/lunch/dinner/drinks), so
    every page that independently clears the threshold is kept and combined —
    keeping only the best page silently dropped the rest of the menu.
    """
    scraped_urls = [p[0] for p in pages]

    # Score each page; pick the highest menu score.
    scored = [(page_url, text, score_menu_text(text)) for page_url, text in pages]
    diagnostics = [
        {
            "stage": crawl_method,
            "url": page_url,
            "chars": len(text),
            "score": score.score,
            "prices": score.price_count,
            "food_words": score.food_word_hits,
            "sections": score.section_hits,
            "decision": "candidate",
        }
        for page_url, text, score in scored
    ]
    best_url, best_text, best = max(scored, key=lambda t: t[2].score)

    if len(best_text) < _MIN_USEFUL_CHARS:
        hint = "likely JS-rendered or a block page"
        if third_party_hosts:
            hint = f"menu on third-party host ({', '.join(third_party_hosts)})"
        return ScrapeResult(
            url=url,
            ok=False,
            status_code=status_code,
            text=best_text,
            char_count=len(best_text),
            scraped_urls=scraped_urls,
            menu_url=best_url,
            third_party_hosts=third_party_hosts,
            menu_score=best.score,
            is_menu=False,
            error=f"Too little text ({len(best_text)} chars) — {hint}. "
            "Photo fallback candidate.",
            diagnostics=diagnostics,
        )

    if not best.is_menu:
        hint = ""
        if third_party_hosts:
            hint = f" Menu may be on third-party host ({', '.join(third_party_hosts)})."
        return ScrapeResult(
            url=url,
            ok=False,
            status_code=status_code,
            text=best_text,
            char_count=len(best_text),
            scraped_urls=scraped_urls,
            menu_url=best_url,
            third_party_hosts=third_party_hosts,
            menu_score=best.score,
            is_menu=False,
            error=f"No real menu found (score {best.score:.2f}): {best.reason}.{hint}",
            diagnostics=diagnostics,
        )

    # Keep every menu-like page, best first, deduped (identical or contained
    # text — e.g. a landing page that embeds the same menu as /menu), capped
    # so a sprawling site can't blow up classification cost.
    kept: list[tuple[str, str]] = []
    total = 0
    for page_url, text, s in sorted(scored, key=lambda t: t[2].score, reverse=True):
        if not s.is_menu or len(text) < _MIN_USEFUL_CHARS:
            continue
        if any(text in kept_text or kept_text in text for _, kept_text in kept):
            continue
        if kept and total + len(text) > _MAX_COMBINED_CHARS:
            continue
        kept.append((page_url, text))
        total += len(text)

    if len(kept) > 1:
        combined = "\n\n".join(f"[page: {u}]\n{t}" for u, t in kept)
    else:
        combined = best_text

    structured_items, structured_categories = _structured_counts(combined)
    kept_urls = {page_url for page_url, _ in kept}
    for diagnostic in diagnostics:
        diagnostic["decision"] = (
            "keep" if diagnostic.get("url") in kept_urls else "reject-lower-quality"
        )

    normalized_parts = sorted(
        {
            re.sub(r"\s+", " ", page_text).strip().casefold()
            for _, page_text in kept
            if page_text.strip()
        }
    )
    content_hash = hashlib.sha256("\n".join(normalized_parts).encode("utf-8")).hexdigest()

    return ScrapeResult(
        url=best_url,
        ok=True,
        status_code=status_code,
        menu_url=best_url,
        scraped_urls=scraped_urls,
        third_party_hosts=third_party_hosts,
        menu_score=best.score,
        is_menu=True,
        text=combined,
        char_count=len(combined),
        pages=kept,
        crawl_method=crawl_method,
        used_learned_context=used_learned_context,
        content_hash=content_hash,
        structured_item_count=structured_items,
        structured_category_count=structured_categories,
        diagnostics=diagnostics,
    )
