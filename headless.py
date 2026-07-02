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
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def is_available() -> bool:
    """True if Playwright is importable (chromium still needs to be installed)."""
    return _AVAILABLE


def fetch_rendered_html(
    url: str,
    *,
    timeout_ms: int = 25_000,
    settle_ms: int = 2_000,
) -> tuple[str | None, str | None]:
    """Load `url` in headless Chromium and return (rendered_html, error).

    timeout_ms bounds navigation; settle_ms is an extra wait after load for
    late client-side rendering (menus often populate after the initial paint).
    """
    if not _AVAILABLE:
        return None, "Playwright not installed"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=_USER_AGENT)
                # "domcontentloaded" then a settle wait is more reliable than
                # "networkidle", which some ad/analytics-heavy sites never hit.
                page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=settle_ms)
                except PlaywrightTimeout:
                    pass  # good enough; grab what rendered
                page.wait_for_timeout(settle_ms)
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
            finally:
                browser.close()
    except PlaywrightTimeout:
        return None, f"Headless timeout after {timeout_ms} ms"
    except PlaywrightError as exc:
        return None, f"Headless error: {exc}"
    except Exception as exc:  # chromium missing, launch failure, etc.
        return None, f"Headless unavailable: {exc}"

    return html, None
