"""PDF freshness guard: skip stale/event menu PDFs.

Pepe's Cantina served a Cinco de Mayo 2023 PDF (untouched for ~3 years) as
its live menu. A menu PDF the restaurant hasn't re-exported in ~18 months is
almost never current, so ingestion skips it. These tests pin the date
extraction and the staleness decision without any network.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdf_menu import is_pdf_stale, pdf_modified_at  # noqa: E402


def _pdf_with_moddate(date_str: str | None) -> bytes:
    """Minimal PDF-ish bytes carrying an optional /ModDate, like the trailer
    dictionaries real exporters (Adobe Illustrator) write."""
    head = b"%PDF-1.4\n"
    if date_str is None:
        return head + b"<< /Producer(test) >>\n"
    return head + b"<< /ModDate(D:" + date_str.encode() + b")/Producer(test) >>\n"


NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


def test_pdf_modified_at_parses_embedded_moddate():
    pdf = _pdf_with_moddate("20230505085313-04'00'")
    dt = pdf_modified_at(pdf)
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2023, 5, 5)


def test_pdf_modified_at_falls_back_to_creationdate():
    pdf = b"%PDF-1.4\n<< /CreationDate(D:20240101120000Z)/Producer(x) >>\n"
    dt = pdf_modified_at(pdf)
    assert dt is not None and dt.year == 2024


def test_pdf_modified_at_none_when_no_date():
    assert pdf_modified_at(_pdf_with_moddate(None)) is None
    assert pdf_modified_at(b"") is None


def test_stale_pdf_flagged_by_embedded_date():
    # The Pepe's case: a 2023 PDF read in mid-2026.
    pdf = _pdf_with_moddate("20230505085313-04'00'")
    stale, reason = is_pdf_stale(pdf, now=NOW)
    assert stale
    assert "2023-05-05" in reason


def test_fresh_pdf_not_flagged():
    pdf = _pdf_with_moddate("20260601120000Z")
    stale, reason = is_pdf_stale(pdf, now=NOW)
    assert not stale
    assert reason is None


def test_reupload_header_rescues_old_embedded_date():
    # A restaurant re-uploads the same file: embedded ModDate stays old, but
    # the HTTP Last-Modified is recent -> newest date wins, not stale.
    pdf = _pdf_with_moddate("20230505085313-04'00'")
    stale, _ = is_pdf_stale(
        pdf, http_last_modified="Mon, 01 Jun 2026 12:00:00 GMT", now=NOW
    )
    assert not stale


def test_unknown_date_is_not_stale():
    # No positive evidence of age -> never rejected.
    stale, reason = is_pdf_stale(_pdf_with_moddate(None), now=NOW)
    assert not stale
    assert reason is None


def test_http_last_modified_alone_can_flag_stale():
    # Image-only PDF with no embedded date, but an old server timestamp.
    pdf = _pdf_with_moddate(None)
    stale, _ = is_pdf_stale(
        pdf, http_last_modified="Fri, 05 May 2023 12:56:05 GMT", now=NOW
    )
    assert stale


def test_fetched_to_text_drops_stale_pdf():
    # The chokepoint guard: a stale PDF's bytes yield no menu text, whatever
    # path fetched it (landing, followed, or learned-route URL).
    import scraper

    stale = scraper._Fetched(
        pdf_bytes=_pdf_with_moddate("20230505085313-04'00'"),
        last_modified="Fri, 05 May 2023 12:56:05 GMT",
    )
    # Force the extractor to prove it's NOT reached for a stale PDF.
    assert scraper._fetched_to_text(stale) == ""


def test_fetched_to_text_keeps_fresh_pdf(monkeypatch):
    import pdf_menu
    import scraper

    monkeypatch.setattr(
        pdf_menu, "extract_pdf_menu_text", lambda _b: "Tacos $4\nBurrito $9"
    )
    fresh = scraper._Fetched(
        pdf_bytes=_pdf_with_moddate("20260601120000Z"),
        last_modified="Mon, 01 Jun 2026 12:00:00 GMT",
    )
    assert "Tacos" in scraper._fetched_to_text(fresh)
