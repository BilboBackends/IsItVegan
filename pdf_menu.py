"""PDF menu extraction (local first, Claude fallback).

Some restaurants publish their menu as a PDF (e.g. a /menu link that redirects
to a PDF file). Our HTML scraper rejects those as non-HTML. This module pulls
menu text out of a PDF's bytes:

1. Local: pypdf extracts embedded text — free, instant. Works for text-based
   PDFs (most menus exported from a design tool still carry a text layer).
2. Fallback: if local extraction yields too little text (image-only / scanned
   PDF with no text layer), send the PDF to Claude, which reads PDFs natively
   (text + layout, no separate OCR). Only the few that need it pay for a call.

Returns extracted text (possibly empty). The caller scores it with menu_score
like any other source, so a junk extraction is rejected downstream.
"""
from __future__ import annotations

import base64
import io

from config import settings

# Below this many chars, assume the local extract failed (image-based PDF).
_MIN_LOCAL_CHARS = 200

_MODEL = "claude-haiku-4-5"


def _fix_letter_spacing(text: str) -> str:
    """Collapse per-glyph spacing some design-tool PDFs produce.

    pypdf renders their kerning as "D e s s e r t s" — one space between
    letters, two-plus between words — which defeats every downstream word
    match (menu scoring saw The Chapman's dessert PDF as "few food words",
    and "1 7" isn't a price). Lines that are mostly single-character tokens
    get their letters rejoined and word gaps restored.
    """
    fixed: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        tokens = stripped.split(" ")
        single_chars = sum(1 for token in tokens if len(token) == 1)
        if len(tokens) >= 6 and single_chars / len(tokens) > 0.7:
            words: list[str] = []
            current: list[str] = []
            for token in stripped.split(" "):
                if token == "":  # the 2nd of a double space: a word gap
                    if current:
                        words.append("".join(current))
                        current = []
                else:
                    current.append(token)
            if current:
                words.append("".join(current))
            fixed.append(" ".join(words))
        else:
            fixed.append(line)
    return "\n".join(fixed)


def _extract_local(pdf_bytes: bytes) -> str:
    """Pull embedded text from a PDF with pypdf. Empty string on failure."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts = []
        for page in reader.pages:
            txt = page.extract_text() or ""
            if txt.strip():
                parts.append(txt.strip())
        return _fix_letter_spacing("\n".join(parts).strip())
    except Exception:
        return ""


def _extract_via_claude(pdf_bytes: bytes) -> str:
    """Send the PDF to Claude and ask for the menu text verbatim.

    Used only when local extraction is too thin (image/scanned PDF). Returns
    empty string if no key / SDK error.
    """
    if not settings.anthropic_api_key:
        return ""
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
        resp = client.messages.create(
            model=_MODEL,
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": "This is a restaurant menu PDF. Transcribe "
                            "all menu items, descriptions, and prices as plain "
                            "text, preserving section headings. Output only the "
                            "menu content — no commentary.",
                        },
                    ],
                }
            ],
        )
        return next((b.text for b in resp.content if b.type == "text"), "").strip()
    except Exception:
        return ""


# Guard: skip Claude on absurdly large PDFs (avoid huge token bills / API limits).
_MAX_PDF_BYTES = 8_000_000  # ~8 MB


def extract_pdf_menu_text(pdf_bytes: bytes) -> str:
    """Extract menu text from PDF bytes: local first, Claude fallback."""
    if not pdf_bytes:
        return ""
    local = _extract_local(pdf_bytes)
    if len(local) >= _MIN_LOCAL_CHARS:
        return local
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        return local  # too big to send; return whatever local got (maybe empty)
    claude = _extract_via_claude(pdf_bytes)
    # Keep whichever is longer — Claude usually wins on image PDFs, but if it
    # errored/returned nothing, fall back to the local text.
    return claude if len(claude) > len(local) else local
