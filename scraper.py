"""Website menu-text scraper (Phase 1 ingestion).

Fetches a restaurant's website and extracts readable text (the raw material
for Claude's Phase 3 dish classification). It does NOT parse dishes here —
menu HTML is too inconsistent for reliable heuristics, and Claude does that
job better downstream. We just get clean text out.

Returns a ScrapeResult so callers can distinguish success from the many ways
a fetch can fail (timeout, 403, JS-only page, non-HTML) and log rather than
silently drop, per CLAUDE.md.

Known limitations (CLAUDE.md open questions), handled as failures for now:
- JS-rendered menus (no server-side text) -> returns little/no text
- PDF menus, third-party ordering iframes (Toast/Square) -> not followed
These are candidates for the photo-only fallback, out of scope for now.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

# A browser-ish UA; some sites 403 the default httpx agent.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 VeganFindBot/0.1"
)

# Tags whose text is never menu content.
_STRIP_TAGS = ["script", "style", "noscript", "svg", "head", "nav", "footer"]

# If extracted text is shorter than this, treat it as a failed scrape
# (usually a JS-only shell or a block page rather than real menu content).
_MIN_USEFUL_CHARS = 200


@dataclass
class ScrapeResult:
    url: str
    ok: bool
    text: str = ""
    error: str | None = None
    status_code: int | None = None
    char_count: int = 0


def _extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    # get_text with a separator keeps menu items on distinct lines; collapse
    # the runs of blank lines the markup leaves behind.
    raw = soup.get_text(separator="\n")
    lines = [line.strip() for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def scrape_menu_text(
    url: str,
    *,
    timeout: float = 20.0,
    mock_html: str | None = None,
) -> ScrapeResult:
    """Fetch `url` and return extracted readable text.

    Pass mock_html to skip the network (testing / fixtures).
    """
    if mock_html is not None:
        text = _extract_text(mock_html)
        return _finish(url, text, status_code=None)

    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        return ScrapeResult(url=url, ok=False, error=f"{type(exc).__name__}: {exc}")

    if resp.status_code >= 400:
        return ScrapeResult(
            url=url,
            ok=False,
            status_code=resp.status_code,
            error=f"HTTP {resp.status_code}",
        )

    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type.lower():
        return ScrapeResult(
            url=url,
            ok=False,
            status_code=resp.status_code,
            error=f"Non-HTML content-type: {content_type or 'unknown'}",
        )

    text = _extract_text(resp.text)
    return _finish(url, text, status_code=resp.status_code)


def _finish(url: str, text: str, status_code: int | None) -> ScrapeResult:
    if len(text) < _MIN_USEFUL_CHARS:
        return ScrapeResult(
            url=url,
            ok=False,
            status_code=status_code,
            text=text,
            char_count=len(text),
            error=(
                f"Too little text ({len(text)} chars) — likely JS-rendered "
                "or a block page. Photo fallback candidate."
            ),
        )
    return ScrapeResult(
        url=url,
        ok=True,
        status_code=status_code,
        text=text,
        char_count=len(text),
    )
