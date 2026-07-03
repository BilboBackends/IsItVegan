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

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from headless import fetch_rendered_html, is_available
from menu_score import MENU_THRESHOLD, score_menu_text

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
)

# Max menu-ish links to follow per site (bounds requests per restaurant).
_MAX_FOLLOW = 5


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
    # The single URL whose text we kept (the best-scoring page).
    menu_url: str | None = None
    # Third-party menu hosts we saw linked but couldn't scrape (JS-rendered).
    third_party_hosts: list[str] = field(default_factory=list)
    # Menu-likeness of the kept text (0..1) and whether it cleared threshold.
    menu_score: float = 0.0
    is_menu: bool = False


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    # get_text with a separator keeps menu items on distinct lines; collapse
    # the runs of blank lines the markup leaves behind.
    raw = soup.get_text(separator="\n")
    lines = [line.strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


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
    return any(hint in blob for hint in _MENU_HINTS)


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
) -> tuple[list[tuple[str, str]] | None, list[str], _Fetched]:
    """Collect (page_url, text) via plain HTTP: landing + followed menu links.

    Returns (pages | None if landing failed, third_party_hosts, landing_meta).
    """
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        landing = _fetch(client, url)
        if landing.html is None:
            return None, [], landing

        menu_links, third_party, _tp_urls = _find_menu_links(landing.html, url)
        # If keyword matching found no menu link, let the cheap LLM pick one
        # from all page links (catches non-obvious labels).
        if not menu_links:
            llm_link = _llm_menu_link(landing.html, url)
            if llm_link:
                menu_links = [llm_link]

        pages: list[tuple[str, str]] = [(url, _extract_text(landing.html))]
        for link in menu_links:
            page = _fetch(client, link)
            text = _fetched_to_text(page)
            if text:
                pages.append((link, text))
    return pages, third_party, landing


def _collect_headless(url: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Collect (page_url, text) via headless browser: landing + menu links.

    Renders the landing page (running its JS), follows same-domain menu links
    by rendering those too. Used only as a fallback when HTTP finds no menu.
    """
    landing_html, err = fetch_rendered_html(url)
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

    max_renders = 6
    pages: list[tuple[str, str]] = [(url, _extract_text(landing_html))]
    queue: list[tuple[str, int]] = [(lk, 1) for lk in menu_links + tp_urls[:2]]
    seen = {_norm(url)} | {_norm(lk) for lk, _ in queue}
    renders = 0

    while queue and renders < max_renders:
        link, hop = queue.pop(0)
        # Ordering-platform SPAs can take several seconds to paint the menu;
        # give followed pages a longer settle than the landing.
        page_html, _ = fetch_rendered_html(link, settle_ms=6_000)
        renders += 1
        if page_html is None:
            continue
        text = _extract_text(page_html)
        if text:
            pages.append((link, text))
        # Expand another hop unless this page already looks confidently like a
        # menu (ordering platforms often score mid-range on marketing copy).
        if hop == 1 and score_menu_text(text).score < 0.75:
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


def scrape_menu_text(
    url: str,
    *,
    timeout: float = 20.0,
    use_headless: bool = True,
    mock_html: str | None = None,
) -> ScrapeResult:
    """Scrape a restaurant site for menu text, following menu links one level.

    Fast path: plain HTTP (landing + followed menu links), scored to keep the
    best page. If that doesn't yield a real menu and use_headless is set, retry
    the whole thing in a headless browser so JS-rendered menus (Toast/Square/
    Clover and modern SPA sites) actually render before extraction.

    Pass mock_html to skip the network entirely (no link-following, no headless).
    """
    if mock_html is not None:
        return _finish(url, [(url, _extract_text(mock_html))], [], status_code=None)

    pages, third_party, landing = _collect_http(url, timeout)

    # If the landing page itself was fetchable, try scoring the HTTP result.
    http_result = None
    if pages is not None:
        http_result = _finish(
            url, pages, third_party, status_code=landing.status_code
        )
        # A merely-adequate score with third-party ordering links around is
        # suspicious (often platform marketing copy, not the menu) — in that
        # case still try headless, which follows the ordering links.
        confident = http_result.ok and (
            http_result.menu_score >= 0.75 or not third_party
        )
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
        url, hl_pages, hl_third_party or third_party, status_code=200
    )
    # Prefer a real menu; otherwise keep the higher-scoring attempt.
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
) -> ScrapeResult:
    """Score every fetched page, keep the most menu-like, decide ok/fail.

    A scrape only succeeds if the best page both has enough text AND clears the
    menu threshold — so homepage marketing copy no longer counts as a menu.
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

    return ScrapeResult(
        url=best_url,
        ok=True,
        status_code=status_code,
        menu_url=best_url,
        scraped_urls=scraped_urls,
        third_party_hosts=third_party_hosts,
        menu_score=best.score,
        is_menu=True,
        text=best_text,
        char_count=len(best_text),
    )
