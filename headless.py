"""Headless-browser fetch fallback (Playwright + Chromium).

Plain HTTP scraping (scraper.py) gets raw HTML, which is empty for
JS-rendered menus — most third-party ordering hosts (Toast/Square/Clover) and
many modern restaurant sites render the menu client-side. This module loads a
page in a real headless browser so that JavaScript runs and the menu appears,
then returns the rendered HTML.

It is deliberately used only as a FALLBACK (see scraper.py): headless is much
slower and heavier than an HTTP GET, so we only pay for it when the fast path
fails to find a menu.

Returns (html, error) — never raises — so the caller can treat it like the
HTTP fetch path.
"""
from __future__ import annotations

# Playwright is an optional/heavy dependency. Import lazily so the rest of the
# pipeline (discovery, HTTP scraping) works even if it isn't installed.
try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    _AVAILABLE = True
except ImportError:  # pragma: no cover - only when playwright not installed
    _AVAILABLE = False


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def is_available() -> bool:
    """True if Playwright is importable (chromium still needs to be installed)."""
    return _AVAILABLE


def _launch(p):
    """Launch a browser that passes common bot checks.

    Prefer the system Chrome channel with automation markers hidden — this
    gets through Cloudflare's "Just a moment..." wall on ordering platforms
    (Toast etc.), which blocks the bundled headless Chromium outright. Fall
    back to bundled Chromium if Chrome isn't installed.
    """
    args = ["--disable-blink-features=AutomationControlled"]
    try:
        return p.chromium.launch(headless=True, channel="chrome", args=args)
    except Exception:
        return p.chromium.launch(headless=True, args=args)


def _text_lines(html: str) -> list[str]:
    """Visible text lines of an HTML document (bs4, not a scraper import —
    that would be circular)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return [
        line.strip()
        for line in soup.get_text("\n").splitlines()
        if line.strip()
    ]


def _new_segment_lines(lines: list[str], seen: set[str]) -> list[str]:
    """Lines that are new against `seen`, PLUS already-seen lines adjacent to
    a new one.

    Menus put names and prices on separate lines, and the same price line
    ("$9.99") legitimately belongs to many dishes — exact-line dedup would
    orphan dishes from their prices. Adjacency keeps a repeated line when
    it travels with new content (a dish block) while whole repeated regions
    (page chrome on every snapshot) still collapse away.
    """
    is_new = [line not in seen for line in lines]
    kept: list[str] = []
    for i, line in enumerate(lines):
        if (
            is_new[i]
            or (i > 0 and is_new[i - 1])
            or (i + 1 < len(lines) and is_new[i + 1])
        ):
            kept.append(line)
    return kept


def _overflow_div(html: str, banked: list[str]) -> str:
    """HTML div holding banked text lines absent from the final DOM.

    Everything seen during scrolling and tab/category navigation gets banked
    as ordered line segments; whatever the final DOM no longer shows is
    re-attached here once (with price-line adjacency preserved). This keeps
    virtualized lists complete without ballooning a 15-category ordering
    page into 15 concatenated copies of the same chrome.
    """
    from html import escape

    present = set(_text_lines(html))
    missing = _new_segment_lines(banked, present)
    if not missing:
        return ""
    return (
        '\n<div id="__virtualized_overflow__">'
        + "".join(f"<p>{escape(line)}</p>" for line in missing)
        + "</div>"
    )


_TAB_SELECTOR = "button, [role='tab'], a, li"

# Collect label/href of every tab candidate in ONE roundtrip; per-element
# inner_text() calls would make each scan take seconds.
_TAB_SCAN_JS = """
els => els.map(e => ({
    text: (e.innerText || '').trim().slice(0, 40),
    href: e.getAttribute('href') || ''
}))
"""


def _click_section_tabs(page, tab_words: tuple[str, ...]) -> list[str]:
    """Click likely menu-section tabs and snapshot the DOM after each click.

    Tabbed menu widgets (common on site-builder pages) keep only the ACTIVE
    section's dishes in the DOM — a passive render sees one section of ten.

    The tab strip is re-scanned after every click because clicking re-renders
    it (mode toggles like Food/Drinks swap the whole tab set). Each round
    clicks the LAST unclicked section label in DOM order: mode toggles sit
    above the section tabs, so deepest-first exhausts every section of the
    current mode before switching modes (which would make the current
    sections disappear unvisited).

    Two kinds of clickables qualify:
    - vocabulary tabs: elements whose label is a known section word
      ("Burgers", "Desserts") with no real href
    - fragment anchors: links whose href stays on THIS page (`#...`, or the
      same path with a fragment — Square Online's category nav). These are
      label-agnostic: category names ("Street Food", "Tamales") can't be
      enumerated, but a same-document click can't navigate away, so clicking
      them all (bounded) is safe.
    Real links to other pages are skipped — navigating is the link-crawler's
    job, not the tab-clicker's.
    """
    from urllib.parse import urljoin, urlparse

    snapshots: list[str] = []
    seen: set[str] = set()
    start_url = page.url
    start_path = urlparse(start_url).path or "/"

    def _is_same_page_fragment(href: str) -> bool:
        if "#" not in href:
            return False
        base = href.split("#", 1)[0]
        if not base:
            return True
        base_path = urlparse(base).path or "/"
        return base_path in ("", "/", start_path)

    for _ in range(20):  # bounds clicks on menus with many sections/modes
        try:
            candidates = page.eval_on_selector_all(_TAB_SELECTOR, _TAB_SCAN_JS)
        except PlaywrightError:
            break
        target_index = None
        target_label = None
        target_fragment_href = None
        # Generous cap: ordering pages stack hundreds of buttons/list items
        # BEFORE their category nav (Square put Tamale Co's 15 categories
        # past index 250). The scan is one JS roundtrip either way.
        for i, c in enumerate(candidates[:800]):
            label = (c.get("text") or "").strip().lower()
            href = c.get("href") or ""
            if not label or len(label) > 28 or label in seen:
                continue
            fragment = _is_same_page_fragment(href)
            if label not in tab_words and not fragment:
                continue
            if href and not fragment and not href.startswith("#"):
                continue
            # keep last match (deepest)
            target_index, target_label = i, label
            target_fragment_href = href if fragment and href not in ("", "#") else None
        if target_index is None:
            break

        seen.add(target_label)
        if target_fragment_href:
            # Category links often live in a HIDDEN nav drawer, so clicking
            # times out on visibility. Navigating to the fragment URL is
            # same-document SPA routing: works regardless of visibility and
            # cannot leave the page (same path by construction).
            try:
                page.goto(
                    urljoin(start_url, target_fragment_href),
                    timeout=15_000,
                    wait_until="domcontentloaded",
                )
            except (PlaywrightError, PlaywrightTimeout):
                continue
            page.wait_for_timeout(1_200)
        else:
            try:
                page.locator(_TAB_SELECTOR).nth(target_index).click(timeout=1_500)
            except (PlaywrightError, PlaywrightTimeout):
                continue  # hidden/covered; marked seen, move on
            page.wait_for_timeout(800)
        # Hash/query changes are same-document SPA routing; only a PATH
        # change means the click truly navigated away.
        if urlparse(page.url).path != start_path:
            try:
                page.go_back()
                page.wait_for_timeout(800)
            except PlaywrightError:
                break
            continue
        try:
            # Categories often lazy-load their items; nudge before snapshot.
            page.mouse.wheel(0, 2_500)
            page.wait_for_timeout(500)
        except PlaywrightError:
            pass
        try:
            snapshots.append(page.content())
        except PlaywrightError:
            continue
    return snapshots


def fetch_rendered_html(
    url: str,
    *,
    timeout_ms: int = 45_000,
    settle_ms: int = 3_000,
    tab_words: tuple[str, ...] = (),
) -> tuple[str | None, str | None]:
    """Load `url` in a headless browser and return (rendered_html, error).

    timeout_ms bounds navigation; settle_ms is an extra wait after load for
    late client-side rendering (menus often populate after the initial paint).
    Waits through Cloudflare bot-check interstitials when they auto-resolve.

    If tab_words is given and the rendered page has little visible text, the
    page's menu-section tabs are clicked one by one and every snapshot is
    concatenated into the returned HTML (see _click_section_tabs).
    """
    if not _AVAILABLE:
        return None, "Playwright not installed"

    try:
        with sync_playwright() as p:
            browser = _launch(p)
            try:
                page = browser.new_page(
                    user_agent=_USER_AGENT,
                    viewport={"width": 1366, "height": 768},
                )
                # "domcontentloaded" then a settle wait is more reliable than
                # "networkidle", which some ad/analytics-heavy sites never hit.
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=settle_ms)
                except PlaywrightTimeout:
                    pass  # good enough; grab what rendered
                page.wait_for_timeout(settle_ms)

                # Cloudflare-style interstitial: give the JS challenge time to
                # auto-resolve (it usually does with a real Chrome channel).
                for _ in range(5):
                    try:
                        title = (page.title() or "").lower()
                    except PlaywrightError:
                        break
                    if "just a moment" not in title and "attention required" not in title:
                        break
                    page.wait_for_timeout(3_000)

                # Lazy AND virtualized lists both defeat single snapshots:
                # lazy content isn't in the DOM until you scroll to it, and
                # virtualized lists REMOVE items that scroll out of view — so
                # no single scroll position ever contains the whole menu.
                # Scroll in steps, banking every text segment seen; content
                # gone from the final DOM gets re-attached to the returned
                # HTML (see _overflow_div / _new_segment_lines).
                banked: list[str] = []
                banked_seen: set[str] = set()

                def _bank(lines: list[str]) -> None:
                    banked.extend(_new_segment_lines(lines, banked_seen))
                    banked_seen.update(lines)

                def _bank_body() -> None:
                    try:
                        body_text = page.inner_text("body")
                    except PlaywrightError:
                        return
                    _bank([l.strip() for l in body_text.splitlines() if l.strip()])

                _bank_body()
                stalls = 0
                for _ in range(14):
                    seen_before = len(banked_seen)
                    try:
                        page.mouse.wheel(0, 1_800)
                    except PlaywrightError:
                        break
                    page.wait_for_timeout(650)
                    _bank_body()
                    stalls = stalls + 1 if len(banked_seen) == seen_before else 0
                    if stalls >= 3:
                        break
                # Sites that redirect (e.g. Clover) may still be navigating when
                # we ask for content; retry a couple times after a short wait.
                html = ""
                for _ in range(3):
                    try:
                        html = page.content()
                        break
                    except PlaywrightError:
                        page.wait_for_timeout(1_500)
                if not html:
                    html = page.content()  # last attempt; let it raise if truly stuck

                # Sparse page + tab words provided: likely a tabbed menu widget
                # showing one section at a time — click through the sections
                # and keep every snapshot.
                if tab_words:
                    try:
                        body_text_len = len(page.inner_text("body"))
                    except PlaywrightError:
                        body_text_len = 0
                    # 6k, not lower: scroll-banking above already fattens the
                    # body, and a page can look rich while showing one
                    # category of fifteen (Square Online's "Top Menu Items").
                    if body_text_len < 6_000:
                        for snapshot in _click_section_tabs(page, tab_words):
                            _bank(_text_lines(snapshot))
                        # Tab/category navigation may have left the page on a
                        # different section — refresh the base snapshot.
                        try:
                            html = page.content()
                        except PlaywrightError:
                            pass

                # Re-attach banked content the final DOM no longer contains
                # (virtualized away, or shown only on other tabs/categories)
                # so extraction sees everything that was ever on screen.
                if banked:
                    html += _overflow_div(html, banked)
            finally:
                browser.close()
    except PlaywrightTimeout:
        return None, f"Headless timeout after {timeout_ms} ms"
    except PlaywrightError as exc:
        return None, f"Headless error: {exc}"
    except Exception as exc:  # chromium missing, launch failure, etc.
        return None, f"Headless unavailable: {exc}"

    return html, None
