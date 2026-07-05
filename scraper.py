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

Known limitations (CLAUDE.md open questions), still handled as failures:
- JS-rendered menus (no server-side text, incl. many third-party ordering
  hosts) -> too little text. Detected third-party hosts are flagged in the
  error so they're clear photo-fallback candidates.
- PDF menus -> non-HTML, not parsed here.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from headless import fetch_rendered_html, is_available
from menu_score import MENU_THRESHOLD, score_menu_text
from structured_menu import extract_structured_menu_text

# A browser-ish UA; some sites 403 the default httpx agent.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 VeganFindBot/0.1"
)

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
    "deserts",  # a common menu misspelling
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
    "sides", "beverages", "drinks", "specials",
)

# Links we never follow even if they look menu-ish (socials / maps / forms).
_SKIP_HINTS = (
    "facebook.", "instagram.", "twitter.", "yelp.", "tel:", "mailto:",
    "maps.app.goo.gl", "google.com/maps", "forms.gle", "youtube.", "tiktok.",
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
)

# Max menu-ish links to follow per page (bounds requests per restaurant).
# Section-per-page menus commonly have 6-8 sections.
_MAX_FOLLOW = 8

# Max total link fetches per site over HTTP (landing excluded). Bounds the
# two-hop expansion of menu index pages (/menu/ -> /menu/lunch, /menu/dinner).
_MAX_HTTP_FETCHES = 12

# Cap on combined kept text across pages — bounds classification cost.
_MAX_COMBINED_CHARS = 50_000

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


def _fetch(client: httpx.Client, url: str) -> _Fetched:
    """Fetch one URL: HTML, PDF bytes, or a classified error (never raises)."""
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        return _Fetched(error=f"{type(exc).__name__}: {exc}")
    if resp.status_code >= 400:
        return _Fetched(status_code=resp.status_code, error=f"HTTP {resp.status_code}")
    content_type = resp.headers.get("content-type", "").lower()
    if "pdf" in content_type:
        # Menus are often PDFs — keep the bytes so a follower can extract them.
        return _Fetched(pdf_bytes=resp.content, status_code=resp.status_code)
    if "html" not in content_type:
        return _Fetched(
            status_code=resp.status_code,
            error=f"Non-HTML content-type: {content_type or 'unknown'}",
        )
    return _Fetched(html=resp.text, status_code=resp.status_code)


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

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#"):
            continue
        if not _looks_menu_like(a.get_text(" ", strip=True), href):
            continue

        absolute = urljoin(base_url, href)
        host = urlparse(absolute).netloc.lower()

        # Any cross-domain menu-looking link is a candidate for headless
        # following — ordering platforms are endless (Toast, Clover, Sauce,
        # MealKeyway, ...) so we don't gate on a known-host list. Known hosts
        # are still named in third_party for failure messages.
        base_bare = base_host.replace("www.", "")
        if host.replace("www.", "") != base_bare:
            tp = next((h for h in _THIRD_PARTY_HOSTS if h in host), None)
            if tp and tp not in third_party:
                third_party.append(tp)
            # Keep the SPA fragment (#/main routes matter on ordering apps);
            # dedupe on the fragment-less form.
            norm_tp = absolute.split("#")[0].rstrip("/")
            if norm_tp not in {u.split("#")[0].rstrip("/") for u in third_party_urls}:
                third_party_urls.append(absolute)
            continue

        # Same-domain: dedup, and don't refetch the landing page itself.
        norm = absolute.split("#")[0].rstrip("/")
        if norm in seen or norm == base_url.split("#")[0].rstrip("/"):
            continue
        seen.add(norm)
        follow.append(absolute)

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
    url: str, timeout: float
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
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        landing = _fetch(client, url)
        if landing.html is None:
            return None, [], [], landing

        menu_links, third_party, tp_urls = _find_menu_links(landing.html, url)
        # If keyword matching found no menu link, let the cheap LLM pick one
        # from all page links (catches non-obvious labels).
        if not menu_links:
            llm_link = _llm_menu_link(landing.html, url)
            if llm_link:
                menu_links = [llm_link]

        def _norm(u: str) -> str:
            return u.split("#")[0].rstrip("/")

        pages: list[tuple[str, str]] = _pages_from_html(url, landing.html)
        queue: list[tuple[str, int]] = [(lk, 1) for lk in menu_links]
        seen = {_norm(url)} | {_norm(lk) for lk in menu_links}
        # "/menu" is a near-universal convention — probe it even when the
        # landing never links to it. JS-built navs hide links from static
        # HTML entirely (F&D Cantina's Popmenu site: the landing shows only
        # order/catering links, while /menu carries the FULL menu as
        # JSON-LD). Costs one request; a 404 is simply skipped.
        conventional = urljoin(url, "/menu")
        if _norm(conventional) not in seen:
            seen.add(_norm(conventional))
            queue.append((conventional, 1))
        fetches = 0
        while queue and fetches < _MAX_HTTP_FETCHES:
            link, hop = queue.pop(0)
            page = _fetch(client, link)
            fetches += 1
            if page.html is not None:
                pages.extend(_pages_from_html(link, page.html))
            else:
                text = _fetched_to_text(page)  # PDFs
                if text:
                    pages.append((link, text))
            if hop == 1 and page.html is not None:
                more_links, more_tp, more_tp_urls = _find_menu_links(page.html, link)
                for host in more_tp:
                    if host not in third_party:
                        third_party.append(host)
                for tp_url in more_tp_urls:
                    if _norm(tp_url) not in {_norm(u) for u in tp_urls}:
                        tp_urls.append(tp_url)
                for nxt in more_links:
                    if _norm(nxt) not in seen:
                        seen.add(_norm(nxt))
                        queue.append((nxt, 2))
    return pages, third_party, tp_urls, landing


def _collect_headless(url: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Collect (page_url, text) via headless browser: landing + menu links.

    Renders the landing page (running its JS), follows same-domain menu links
    by rendering those too. Used only as a fallback when HTTP finds no menu.
    """
    # tab_words on the landing too: for single-page ordering sites (Square
    # Online et al.) the landing IS the menu, with category tabs/fragments.
    landing_html, err = fetch_rendered_html(url, tab_words=_TAB_WORDS)
    if landing_html is None:
        return [], []

    menu_links, third_party, tp_urls = _find_menu_links(landing_html, url)
    if not menu_links and not tp_urls:
        llm_link = _llm_menu_link(landing_html, url)
        if llm_link:
            menu_links = [llm_link]

    # Third-party ordering pages (Toast/Clover/Sauce...) render their menu in
    # the DOM once the real-Chrome launch clears their bot wall — follow them
    # like any other menu link (capped: they're slow). Ordering platforms often
    # interpose a marketing page before the actual menu, so pages that still
    # don't look like a menu get ONE more hop of menu-like links.
    def _norm(u: str) -> str:
        return u.split("#")[0].rstrip("/")

    max_renders = 9
    pages: list[tuple[str, str]] = _pages_from_html(url, landing_html)
    queue: list[tuple[str, int]] = [(lk, 1) for lk in menu_links + tp_urls[:2]]
    seen = {_norm(url)} | {_norm(lk) for lk, _ in queue}
    renders = 0

    while queue and renders < max_renders:
        link, hop = queue.pop(0)
        # Ordering-platform SPAs can take several seconds to paint the menu;
        # give followed pages a longer settle than the landing. Followed pages
        # are menu candidates, so sparse ones get their section tabs clicked.
        page_html, _ = fetch_rendered_html(link, settle_ms=6_000, tab_words=_TAB_WORDS)
        renders += 1
        if page_html is None:
            continue
        page_candidates = _pages_from_html(link, page_html)
        pages.extend(page_candidates)
        # Expand another hop unless this page already looks confidently like a
        # menu (ordering platforms often score mid-range on marketing copy).
        # A structured pseudo-page counts: embedded menu data IS the menu.
        best_here = max(
            score_menu_text(candidate_text).score
            for _, candidate_text in page_candidates
        )
        if hop == 1 and best_here < 0.75:
            more_menu, _tp, more_tp_urls = _find_menu_links(page_html, link)
            # Links literally containing "menu" first — on ordering sites every
            # URL contains /order/, so the generic hint matches everything.
            candidates = sorted(
                more_menu + more_tp_urls,
                key=lambda u: 0 if "menu" in u.lower() else 1,
            )
            for nxt in candidates[:2]:
                if _norm(nxt) not in seen:
                    seen.add(_norm(nxt))
                    queue.append((nxt, 2))

    return pages, third_party


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
    urls: list[str], timeout: float
) -> list[tuple[str, str]]:
    """Fetch previously validated menu pages directly, skipping discovery."""
    pages: list[tuple[str, str]] = []
    seen: set[str] = set()
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        for saved_url in urls[:_MAX_HTTP_FETCHES]:
            # #structured-menu is our synthetic source marker, not a real
            # server route. Fetching its base recreates both DOM and embedded
            # structured candidates.
            fetch_url = saved_url.removesuffix("#structured-menu")
            normalized = fetch_url.rstrip("/")
            if normalized in seen:
                continue
            seen.add(normalized)
            page = _fetch(client, fetch_url)
            if page.html is not None:
                pages.extend(_pages_from_html(fetch_url, page.html))
            else:
                text = _fetched_to_text(page)
                if text:
                    pages.append((fetch_url, text))
    return pages


def _collect_known_headless(urls: list[str]) -> list[tuple[str, str]]:
    """Render previously validated JS menu pages directly."""
    pages: list[tuple[str, str]] = []
    seen: set[str] = set()
    for saved_url in urls[:9]:
        fetch_url = saved_url.removesuffix("#structured-menu")
        normalized = fetch_url.rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        html, _ = fetch_rendered_html(
            fetch_url, settle_ms=6_000, tab_words=_TAB_WORDS
        )
        if html is not None:
            pages.extend(_pages_from_html(fetch_url, html))
    return pages


def _try_learned_context(
    home_url: str,
    crawl_context: dict | None,
    *,
    timeout: float,
    use_headless: bool,
) -> ScrapeResult | None:
    """Try the last successful route; return None when rediscovery is needed."""
    urls = _profile_urls(crawl_context)
    method = (crawl_context or {}).get("crawl_method")
    if not urls or method not in {"http", "headless"}:
        return None
    if method == "headless":
        if not use_headless or not is_available():
            return None
        pages = _collect_known_headless(urls)
    else:
        pages = _collect_known_http(urls, timeout)
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
    if not result.ok:
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


def scrape_menu_text(
    url: str,
    *,
    timeout: float = 20.0,
    use_headless: bool = True,
    mock_html: str | None = None,
    crawl_context: dict | None = None,
) -> ScrapeResult:
    """Scrape a restaurant site for menu text, following menu links one level.

    Fast path: plain HTTP (landing + followed menu links), scored to keep the
    best page. If that doesn't yield a real menu and use_headless is set, retry
    the whole thing in a headless browser so JS-rendered menus (Toast/Square/
    Clover and modern SPA sites) actually render before extraction.

    Pass mock_html to skip the network entirely (no link-following, no headless).
    """
    if mock_html is not None:
        return _finish(
            url,
            [(url, _extract_text(mock_html))],
            [],
            status_code=None,
            crawl_method="mock",
        )

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
    )
    if learned is not None:
        return learned

    pages, third_party, tp_urls, landing = _collect_http(url, timeout)

    # If the landing page itself was fetchable, try scoring the HTTP result.
    http_result = None
    if pages is not None:
        http_result = _finish(
            url,
            pages,
            third_party,
            status_code=landing.status_code,
            crawl_method="http",
        )
        # A merely-adequate score with third-party ordering links around is
        # suspicious (often homepage/marketing copy scoring on section names,
        # not the menu) — still try headless, which renders the ordering
        # links. ANY cross-domain menu link counts, not just known platforms:
        # ordering hosts are endless (activemenus, MealKeyway, ...).
        # A tiny "menu" isn't trusted either, whatever it scored — a real
        # menu is rarely under ~1200 chars; it's usually a teaser with the
        # full menu behind JS.
        confident = http_result.ok and (
            http_result.menu_score >= 0.75 or not (third_party or tp_urls)
        ) and http_result.char_count >= 1200
        # A single kept page whose URL names one menu section (/breakfast) is
        # probably a partial menu with the other sections behind JS-rendered
        # nav — escalate to headless to find them.
        if (
            confident
            and len(http_result.pages) == 1
            and _is_single_section_url(http_result.pages[0][0])
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

    hl_pages, hl_third_party = _collect_headless(url)
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
    )
