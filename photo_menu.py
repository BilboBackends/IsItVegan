"""Photo-menu fallback: transcribe menu images with Claude vision.

Some restaurants publish their menu only as an image — a designed JPEG/PNG on
the website with no text layer, no JSON-LD, and no ordering API (The Neighbors
Orlando case: a 1619x2544 "Food Menu" image on a Square Online page whose DOM
holds nothing but cart chrome). The text scraper honestly fails there. This
module closes the gap:

1. Fetch the restaurant's known menu pages (learned crawl route first, site
   root as fallback) and mine their HTML for menu-looking <img> tags.
2. Read each candidate image, cheapest capable tier first (mirroring
   pdf_menu.py's local-then-Claude ladder):
   a. Google Cloud Vision OCR (~$1.50/1000 images). The downstream DeepSeek
      classification already reasons over messy scraped text, so raw OCR
      text is a perfectly good source when it scores like a menu.
   b. Claude vision transcription (~$0.05/image) only when OCR is
      unavailable or its text doesn't score like a menu (multi-column
      scrambling, stylized fonts, or a non-menu image).
3. Score, persist, and record the crawl exactly like a text scrape, so
   downstream classification (DeepSeek), menu versioning, and the Admin
   pipeline treat a photo menu like any other menu.

Independently runnable stage (like discovery/ingest/classify):

    python photo_menu.py --restaurant 404             # transcribe + persist
    python photo_menu.py --restaurant 404 --dry-run   # show, store nothing
    python photo_menu.py --restaurant 404 --classify  # then classify via DeepSeek
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

import db
from config import settings
from menu_score import score_menu_text
from scraper import _HTTP_HEADERS

# Candidate filtering. Menu images are big; icons, sprites, and nav glyphs
# (Square ships a literal menu.svg hamburger) are not.
_MIN_IMAGE_BYTES = 30_000
_MAX_IMAGE_BYTES = 12_000_000
_MAX_IMAGES_PER_RESTAURANT = 4
_IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# A transcription shorter than this is a sign the "menu" image was a teaser
# (hours board, single special) — reject rather than classify a fragment.
_MIN_MENU_CHARS = 200

# OCR text below this menu score doesn't get trusted — it's either a non-menu
# image (OCR can't judge that; Claude's is_menu gate can) or a layout OCR
# scrambled badly enough to defeat price/dish pairing. Matches the audit's
# WEAK_SCORE bar for "reads like a menu".
_OCR_ACCEPT_SCORE = 0.60

_OCR_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"

# Final rung for images the cheap tiers misread — one Opus retry beats
# classifying a bad transcription.
_ESCALATION_MODEL = "claude-opus-4-8"

# Three or more consecutive lines that are nothing but a price means the OCR
# linearized a two-column layout by splitting the price column away from its
# dishes (The Neighbors: six dishes, then "8 9 12 10 10 15"). The dish/verdict
# text survives, but every price would be lost or misattributed — that's a
# Claude-tier image. Alternating dish/price lines never trigger this.
_BARE_PRICE_RE = re.compile(r"^\$?\d{1,3}(?:\.\d{2})?$")
_MAX_BARE_PRICE_RUN = 2


def _price_column_detached(text: str) -> bool:
    run = 0
    for line in text.splitlines():
        if _BARE_PRICE_RE.match(line.strip()):
            run += 1
            if run > _MAX_BARE_PRICE_RUN:
                return True
        else:
            run = 0
    return False


# ---------------------------------------------------------------------------
# Geometry repair: re-attach detached price columns using the word bounding
# boxes DOCUMENT_TEXT_DETECTION already returns. A price rendered on the same
# visual row as its dish belongs to that dish — pure deterministic Python,
# no extra API spend, and the reason most two-column menus never need Claude.
# ---------------------------------------------------------------------------

def _box_bounds(box: dict) -> tuple[int, int, int]:
    vertices = box.get("vertices") or [{}]
    ys = [v.get("y", 0) for v in vertices]
    xs = [v.get("x", 0) for v in vertices]
    return min(ys), max(ys), min(xs)


def _ocr_lines(annotation: dict) -> list[dict]:
    """Flatten fullTextAnnotation into OCR's own lines with row geometry."""
    lines: list[dict] = []
    words: list[str] = []
    ymin = ymax = None
    xmin = None

    def flush() -> None:
        nonlocal words, ymin, ymax, xmin
        if words:
            lines.append(
                {"text": " ".join(words), "ymin": ymin, "ymax": ymax,
                 "xmin": xmin}
            )
        words, ymin, ymax, xmin = [], None, None, None

    for page in annotation.get("pages", []):
        for block in page.get("blocks", []):
            for paragraph in block.get("paragraphs", []):
                for word in paragraph.get("words", []):
                    symbols = word.get("symbols", [])
                    text = "".join(s.get("text", "") for s in symbols)
                    if text:
                        words.append(text)
                        w_ymin, w_ymax, w_xmin = _box_bounds(
                            word.get("boundingBox", {})
                        )
                        ymin = w_ymin if ymin is None else min(ymin, w_ymin)
                        ymax = w_ymax if ymax is None else max(ymax, w_ymax)
                        xmin = w_xmin if xmin is None else min(xmin, w_xmin)
                    brk = (
                        (symbols[-1].get("property") or {})
                        .get("detectedBreak", {})
                        .get("type")
                        if symbols
                        else None
                    )
                    if brk in ("LINE_BREAK", "EOL_SURE_SPACE"):
                        flush()
                flush()
    return lines


def _reattach_detached_prices(lines: list[dict]) -> str | None:
    """Rejoin price-only lines to the content line sharing their visual row.

    Fail-open by design: a price line that overlaps no content row (the
    alternating dish-then-price layout) stays exactly where OCR put it, and
    anything without usable geometry returns None so the raw text stands.
    """
    price_lines = [
        line for line in lines if _BARE_PRICE_RE.match(line["text"].strip())
    ]
    content = [
        line for line in lines
        if not _BARE_PRICE_RE.match(line["text"].strip())
    ]
    if not price_lines or not content:
        return None

    attached: dict[int, list[str]] = {}
    placed: set[int] = set()
    for index, price in enumerate(price_lines):
        height = max(1, (price["ymax"] or 0) - (price["ymin"] or 0))
        best, best_overlap = None, 0
        for target_index, line in enumerate(content):
            overlap = min(price["ymax"], line["ymax"]) - max(
                price["ymin"], line["ymin"]
            )
            # Same visual row, price sitting to the right of the text.
            if overlap > best_overlap and (line["xmin"] or 0) < (price["xmin"] or 0):
                best, best_overlap = target_index, overlap
        if best is not None and best_overlap >= 0.5 * height:
            attached.setdefault(best, []).append(price["text"].strip())
            placed.add(index)

    if not placed:
        return None
    out: list[str] = []
    price_positions = {id(line): i for i, line in enumerate(price_lines)}
    content_positions = {id(line): i for i, line in enumerate(content)}
    for line in lines:
        price_index = price_positions.get(id(line))
        if price_index is not None:
            if price_index not in placed:
                out.append(line["text"])
            continue
        content_index = content_positions[id(line)]
        suffix = " ".join(attached.get(content_index, []))
        out.append(f"{line['text']} {suffix}".rstrip())
    return "\n".join(out)

_MENU_WORDS = ("menu", "carte", "speisekarte")
_NOT_MENU_WORDS = ("logo", "icon", "sprite", "banner-home", "favicon")

_SYSTEM = """You transcribe restaurant menu images into plain text.

Rules:
- Transcribe verbatim: every section heading, dish name, description, and
  price, in reading order. Keep one dish per line with its description and
  price on the same or following line, matching the layout's grouping.
- Do not invent, summarize, or normalize anything. If a word is illegible,
  write [illegible] rather than guessing.
- Ignore purely decorative text (social handles, "follow us", photo credits).
- is_menu is true only when the image is actually a food or drink menu —
  a storefront photo, dish photo, or flyer without orderable items is false.
"""

_SCHEMA = {
    "type": "object",
    "required": ["is_menu", "menu_text"],
    "additionalProperties": False,
    "properties": {
        "is_menu": {
            "type": "boolean",
            "description": "True only if the image shows an actual menu.",
        },
        "menu_text": {
            "type": "string",
            "description": "Verbatim transcription; empty when is_menu is false.",
        },
    },
}

# $/MTok for the vision model; mirrors classification_providers.PRICES intent.
_VISION_PRICES = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


@dataclass
class Transcription:
    ok: bool
    is_menu: bool = False
    text: str = ""
    error: str | None = None
    cost_estimate: float = 0.0
    method: str = "claude"  # "ocr" | "claude"


@dataclass
class PhotoMenuResult:
    ok: bool
    error: str | None = None
    pages: list[tuple[str, str]] = field(default_factory=list)
    menu_score: float = 0.0
    char_count: int = 0
    cost_estimate: float = 0.0
    images_seen: int = 0


def find_menu_image_urls(page_url: str, html: str) -> list[str]:
    """Menu-looking <img> URLs in one page's HTML, absolute and deduped.

    Generic on purpose: an image is a candidate when 'menu' (or a cognate)
    appears in its alt text or src filename and no anti-signal ('logo',
    'icon', …) does. Size/type junk is filtered later at download time.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    urls: list[str] = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        alt = img.get("alt") or ""
        haystack = f"{alt} {src}".lower()
        if not any(word in haystack for word in _MENU_WORDS):
            continue
        if any(word in haystack for word in _NOT_MENU_WORDS):
            continue
        absolute = urljoin(page_url, src)
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
    return urls


def _candidate_page_urls(restaurant: dict, db_path: str | None = None) -> list[str]:
    """Learned menu pages first (the scraper already found where the menu
    lives, even if only its image), then the site root as discovery."""
    urls: list[str] = []
    profile = db.get_crawl_profile(restaurant["id"], db_path=db_path)
    if profile and profile.get("menu_urls"):
        urls.extend(profile["menu_urls"])
    website = restaurant.get("website_url")
    if website and website not in urls:
        urls.append(website)
    return urls


def _fetch(url: str) -> httpx.Response | None:
    try:
        return httpx.get(
            url, headers=_HTTP_HEADERS, follow_redirects=True, timeout=30
        )
    except httpx.HTTPError:
        return None


def _fetch_rendered(url: str) -> tuple[str | None, str | None]:
    """Headless render for pages whose <img> tags only exist client-side."""
    from headless import fetch_rendered_html

    return fetch_rendered_html(url)


def _download_image(url: str) -> tuple[bytes, str] | None:
    """(bytes, media_type) for a plausible menu image, else None."""
    response = _fetch(url)
    if response is None or response.status_code != 200:
        return None
    media_type = (
        response.headers.get("content-type", "").split(";")[0].strip().lower()
    )
    if media_type not in _IMAGE_MEDIA_TYPES:
        return None
    data = response.content
    if not (_MIN_IMAGE_BYTES <= len(data) <= _MAX_IMAGE_BYTES):
        return None
    return data, media_type


def ocr_menu_image(image_bytes: bytes) -> Transcription:
    """Cheap tier: Google Cloud Vision document OCR.

    Returns ok=False when the API is unavailable/unauthorized (key without
    the Vision API enabled) so the caller falls through to Claude. is_menu
    is decided by the caller via menu scoring — OCR has no judgment.
    """
    if not settings.google_vision_api_key:
        return Transcription(ok=False, error="No Google Vision API key",
                             method="ocr")
    import base64

    try:
        response = httpx.post(
            _OCR_ENDPOINT,
            params={"key": settings.google_vision_api_key},
            json={
                "requests": [
                    {
                        "image": {
                            "content": base64.standard_b64encode(
                                image_bytes
                            ).decode("utf-8")
                        },
                        "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                    }
                ]
            },
            timeout=60,
        )
    except httpx.HTTPError as exc:
        return Transcription(ok=False, error=f"OCR request failed: {exc}",
                             method="ocr")
    if response.status_code != 200:
        return Transcription(
            ok=False, method="ocr",
            error=f"OCR HTTP {response.status_code}: {response.text[:200]}",
        )
    body = response.json().get("responses", [{}])[0]
    if body.get("error"):
        return Transcription(
            ok=False, method="ocr",
            error=f"OCR error: {body['error'].get('message', 'unknown')}",
        )
    annotation = body.get("fullTextAnnotation") or {}
    text = annotation.get("text", "").strip()
    try:
        repaired = _reattach_detached_prices(_ocr_lines(annotation))
        if repaired:
            text = repaired
    except Exception:
        pass  # geometry repair is best-effort; the raw text stands
    # $1.50 per 1000 images after the monthly free tier.
    return Transcription(ok=True, text=text, cost_estimate=0.0015,
                         method="ocr")


def _passes_menu_gates(result: Transcription) -> bool:
    return result.ok and result.is_menu and len(result.text) >= _MIN_MENU_CHARS


def read_menu_image(
    image_bytes: bytes, media_type: str, *, model: str | None = None
) -> Transcription:
    """Cheapest capable reader for one menu image, escalating rung by rung.

    1. OCR (+ deterministic geometry repair), accepted when the text is
       substantial, scores like a menu, and keeps prices with their dishes.
    2. Claude vision on the configured model (Haiku by default — the
       pdf_menu.py tier for exactly this transcription job).
    3. One retry on the escalation model when the cheap read fails the
       menu gates — stylized or degraded images that defeat Haiku.
    """
    ocr = ocr_menu_image(image_bytes)
    if ocr.ok:
        if (
            len(ocr.text) >= _MIN_MENU_CHARS
            and score_menu_text(ocr.text).score >= _OCR_ACCEPT_SCORE
            and not _price_column_detached(ocr.text)
        ):
            ocr.is_menu = True
            return ocr
    elif ocr.error and "No Google Vision API key" not in ocr.error:
        print(f"  [photo] OCR tier unavailable: {ocr.error}")

    model = model or settings.photo_menu_vision_model
    claude = transcribe_menu_image(image_bytes, media_type, model=model)
    claude.cost_estimate += ocr.cost_estimate  # the failed OCR try still billed
    if _passes_menu_gates(claude) or model == _ESCALATION_MODEL:
        return claude
    escalated = transcribe_menu_image(
        image_bytes, media_type, model=_ESCALATION_MODEL
    )
    escalated.cost_estimate += claude.cost_estimate
    return escalated


def transcribe_menu_image(
    image_bytes: bytes, media_type: str, *, model: str | None = None
) -> Transcription:
    """One Claude vision call: menu image -> verbatim transcription."""
    if not settings.anthropic_api_key:
        return Transcription(ok=False, error="ANTHROPIC_API_KEY not set")
    model = model or settings.photo_menu_vision_model
    try:
        import anthropic
        import base64

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        kwargs = dict(
            model=model,
            max_tokens=16000,
            system=_SYSTEM,
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
        # Adaptive thinking is a 4.6+ parameter; Haiku rejects it.
        if "haiku" not in model:
            kwargs["thinking"] = {"type": "adaptive"}
        with client.messages.stream(
            **kwargs,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.standard_b64encode(
                                    image_bytes
                                ).decode("utf-8"),
                            },
                        },
                        {
                            "type": "text",
                            "text": "Transcribe this restaurant menu image.",
                        },
                    ],
                }
            ],
        ) as stream:
            response = stream.get_final_message()
    except Exception as exc:
        return Transcription(ok=False, error=f"{type(exc).__name__}: {exc}")

    usage = getattr(response, "usage", None)
    input_price, output_price = _VISION_PRICES.get(model, (5.0, 25.0))
    cost = (
        (getattr(usage, "input_tokens", 0) or 0) * input_price
        + (getattr(usage, "output_tokens", 0) or 0) * output_price
    ) / 1_000_000
    if response.stop_reason == "refusal":
        return Transcription(ok=False, error="Model refused", cost_estimate=cost)
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
        return Transcription(
            ok=True,
            is_menu=bool(data.get("is_menu")),
            text=str(data.get("menu_text") or "").strip(),
            cost_estimate=cost,
        )
    except (json.JSONDecodeError, AttributeError) as exc:
        return Transcription(
            ok=False, error=f"Malformed response: {exc}", cost_estimate=cost
        )


def run(
    restaurant_id: int,
    *,
    dry_run: bool = False,
    db_path: str | None = None,
) -> PhotoMenuResult:
    restaurant = next(
        (r for r in db.list_restaurants(db_path=db_path) if r["id"] == restaurant_id),
        None,
    )
    if restaurant is None:
        return PhotoMenuResult(ok=False, error=f"No restaurant {restaurant_id}.")

    # 1. Collect candidate image URLs from the restaurant's known pages.
    #    Raw HTML first; when a page ships its content client-side (the same
    #    Vue/ordering-platform sites whose text the scraper can't see), fall
    #    back to a headless render of that page — mirroring scraper.py's
    #    http-then-headless ladder.
    image_urls: list[str] = []

    def collect(page_url: str, html: str) -> None:
        for url in find_menu_image_urls(page_url, html):
            if url not in image_urls:
                image_urls.append(url)

    for page_url in _candidate_page_urls(restaurant, db_path=db_path):
        response = _fetch(page_url)
        if response is not None and response.status_code == 200:
            collect(str(response.url), response.text)
        if not image_urls:
            rendered, _error = _fetch_rendered(page_url)
            if rendered:
                collect(page_url, rendered)
    if not image_urls:
        return PhotoMenuResult(ok=False, error="No menu-looking images found.")

    # 2. Transcribe each candidate; keep the ones Claude confirms are menus.
    pages: list[tuple[str, str]] = []
    cost = 0.0
    seen = 0
    for url in image_urls[:_MAX_IMAGES_PER_RESTAURANT]:
        downloaded = _download_image(url)
        if downloaded is None:
            continue
        seen += 1
        image_bytes, media_type = downloaded
        result = read_menu_image(image_bytes, media_type)
        cost += result.cost_estimate
        if result.ok and result.is_menu and len(result.text) >= _MIN_MENU_CHARS:
            print(f"  [photo] {url[:80]}: kept via {result.method}")
            pages.append((url, result.text))
        elif result.error:
            print(f"  [photo] {url[:80]}: {result.error}")
    if not pages:
        return PhotoMenuResult(
            ok=False,
            error="No image transcribed into a plausible menu.",
            cost_estimate=cost,
            images_seen=seen,
        )

    # 3. Persist exactly like a successful text scrape so classification,
    #    versioning, and change tracking need no special photo handling.
    combined = "\n\n".join(
        f"[menu image: {url}]\n{text}" for url, text in pages
    )
    score = score_menu_text(combined)
    content_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    if not dry_run:
        db.replace_menu_texts(restaurant_id, pages, fetched_at=now, db_path=db_path)
        db.record_menu_version(
            restaurant_id,
            combined,
            content_hash,
            menu_score=score.score,
            char_count=len(combined),
            fetched_at=now,
            db_path=db_path,
        )
        db.record_crawl_success(
            restaurant_id,
            menu_urls=[url for url, _ in pages],
            crawl_method="photo",
            content_hash=content_hash,
            menu_score=score.score,
            char_count=len(combined),
            crawled_at=now,
            db_path=db_path,
        )
    return PhotoMenuResult(
        ok=True,
        pages=pages,
        menu_score=score.score,
        char_count=len(combined),
        cost_estimate=cost,
        images_seen=seen,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restaurant", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="transcribe but store nothing")
    parser.add_argument("--classify", action="store_true",
                        help="run DeepSeek classification after a successful "
                             "transcription")
    args = parser.parse_args()

    db.init_db()
    result = run(args.restaurant, dry_run=args.dry_run)
    if not result.ok:
        raise SystemExit(f"photo_menu failed: {result.error}")
    print(
        f"Transcribed {len(result.pages)} menu image(s): "
        f"{result.char_count} chars, score {result.menu_score:.2f}, "
        f"${result.cost_estimate:.2f}"
        f"{' (dry run — nothing stored)' if args.dry_run else ''}"
    )
    if args.classify and not args.dry_run:
        import classify

        classify.run(restaurant_id=args.restaurant)


if __name__ == "__main__":
    main()
