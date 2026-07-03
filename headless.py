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
    sections disappear unvisited). Real links are skipped — navigating is the
    link-crawler's job, not the tab-clicker's.
    """
    snapshots: list[str] = []
    seen: set[str] = set()
    start_url = page.url

    for _ in range(20):  # bounds clicks on menus with many sections/modes
        try:
            candidates = page.eval_on_selector_all(_TAB_SELECTOR, _TAB_SCAN_JS)
        except PlaywrightError:
            break
        target_index = None
        target_label = None
        for i, c in enumerate(candidates[:250]):
            label = (c.get("text") or "").strip().lower()
            href = c.get("href") or ""
            if not label or len(label) > 28 or label in seen or label not in tab_words:
                continue
            if href and not href.startswith("#"):
                continue
            target_index, target_label = i, label  # keep last match (deepest)
        if target_index is None:
            break

        seen.add(target_label)
        try:
            page.locator(_TAB_SELECTOR).nth(target_index).click(timeout=1_500)
        except (PlaywrightError, PlaywrightTimeout):
            continue  # hidden/covered; marked seen, move on
        page.wait_for_timeout(800)
        if page.url != start_url:
            # The click navigated after all; back out and keep going.
            try:
                page.go_back()
                page.wait_for_timeout(800)
            except PlaywrightError:
                break
            continue
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

                # Nudge lazy-loaded content (long menus often render on scroll).
                try:
                    page.mouse.wheel(0, 4_000)
                    page.wait_for_timeout(1_500)
                except PlaywrightError:
                    pass
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
                    if body_text_len < 3_000:
                        snapshots = _click_section_tabs(page, tab_words)
                        if snapshots:
                            html = "\n".join([html, *snapshots])
            finally:
                browser.close()
    except PlaywrightTimeout:
        return None, f"Headless timeout after {timeout_ms} ms"
    except PlaywrightError as exc:
        return None, f"Headless error: {exc}"
    except Exception as exc:  # chromium missing, launch failure, etc.
        return None, f"Headless unavailable: {exc}"

    return html, None
