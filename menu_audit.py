"""Automated menu-quality audit — the "no deep dive needed" guardrail.

Every scraper failure mode we've debugged by hand had a signature visible in
the stored data itself: a suspiciously tiny menu, menu text with no prices,
only a single section of a multi-section menu, several restaurants sharing
identical text (a chain's generic ordering page), or a restaurant with a
website but nothing scraped. This module turns those signatures into checks
that run over the database, so new offenders and regressions surface as a
short reviewable list (Admin shows it; tests assert on the logic) instead of
requiring someone to re-derive the analysis.

Deliberately read-only and heuristic: a flag is a *prompt to look*, not a
verdict. Flags come with the evidence (chars, score, the duplicated partner)
so the follow-up is one click, not an investigation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time

import db
from config import settings
from menu_score import score_menu_text
from venue_filter import is_consumer_food_venue

# Below this, a stored "menu" is almost certainly a teaser or a fragment
# (matches the scraper's own confidence floor).
MIN_PLAUSIBLE_CHARS = 1200

# A stored menu whose score sits barely above the keep threshold is usually
# marketing copy that squeaked past on section names (the Pickles case).
WEAK_SCORE = 0.60

# A menu that was CLASSIFIED but yielded fewer dishes than this is usually an
# incomplete capture that went live (teaser page, single daypart) — even when
# the fragment reads well enough to pass the text checks above. Genuinely
# tiny menus get one "Menu is correct" review and stay hidden.
MIN_PLAUSIBLE_DISHES = 10

_PRICE_RE = re.compile(r"\$\s?\d{1,3}(?:\.\d{2})?")
_DYNAMIC_MENU_PLACEHOLDER_RE = re.compile(
    r"\b(?:loading\s+(?:our\s+)?menus?|menus?\s+(?:are\s+)?loading)\b",
    re.IGNORECASE,
)

# Mirrors scraper._SECTION_PATH_RE_WORDS without importing scraper (which
# pulls in headless/playwright); keep the two lists aligned.
_SECTION_WORDS = (
    "breakfast", "lunch", "dinner", "brunch", "sandwiches", "salads", "soups",
    "wraps", "desserts", "deserts", "burgers", "appetizers", "starters",
    "sides", "beverages", "drinks", "specials",
)

# Auditing reads every raw menu. Keep the small result in memory between
# Admin refreshes, while tying it to SQLite/WAL file metadata so a scrape,
# review, restaurant edit, or deletion invalidates it immediately. The TTL is
# a fallback for unusual filesystems with coarse timestamp behavior.
AUDIT_CACHE_TTL_SECONDS = 60.0
_audit_cache: dict[str, tuple[tuple, float, list[dict]]] = {}
_audit_cache_lock = threading.Lock()


def _database_token(path: str) -> tuple:
    token = []
    for candidate in (path, f"{path}-wal"):
        try:
            stat = os.stat(candidate)
            token.append((candidate, stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            token.append((candidate, None, None))
    return tuple(token)


def clear_audit_cache(db_path: str | None = None) -> None:
    """Clear one audit cache (or all caches); primarily useful in tests."""
    with _audit_cache_lock:
        if db_path is None:
            _audit_cache.clear()
        else:
            _audit_cache.pop(os.path.abspath(db_path), None)


def _is_single_section_url(url: str) -> bool:
    from urllib.parse import urlparse

    path = urlparse(url or "").path.lower()
    return any(
        re.search(rf"(?<![a-z]){w}(?![a-z])", path) for w in _SECTION_WORDS
    )


def _audit_menus_uncached(db_path: str | None = None) -> list[dict]:
    """Audit every consumer-facing restaurant's stored menu text.

    Returns [{restaurant_id, name, flags: [str, ...]}, ...] for restaurants
    with at least one flag, worst-first (most flags first).
    """
    restaurants = [
        r for r in db.list_restaurants(db_path) if is_consumer_food_venue(r)
    ]
    reviews = db.list_menu_quality_reviews(db_path)
    sources_by_restaurant = db.get_menu_sources_by_restaurant(
        [restaurant["id"] for restaurant in restaurants], db_path=db_path
    )
    classified_totals = {
        rid: entry["total"]
        for rid, entry in db.verdict_counts_by_restaurant(db_path).items()
    }
    content_owner: dict[str, tuple[int, str]] = {}  # hash -> (id, name)
    findings: list[dict] = []

    for r in restaurants:
        rid = r["id"]
        sources = sources_by_restaurant.get(rid, [])
        flags: list[str] = []

        if not sources:
            if r.get("website_url"):
                flags.append("website exists but no menu scraped")
            # No website and no menu: photo-fallback case, not a scraper
            # regression — don't flag.
        else:
            combined = "\n".join(s["content"] or "" for s in sources)
            score_value = score_menu_text(combined).score
            prices = len(_PRICE_RE.findall(combined))

            if len(combined) < MIN_PLAUSIBLE_CHARS:
                flags.append(f"menu suspiciously small ({len(combined)} chars)")
            if prices == 0:
                flags.append("no prices anywhere in menu text")
            if score_value < WEAK_SCORE:
                flags.append(
                    f"weak menu score ({score_value:.2f}) — may be "
                    "marketing copy, not a menu"
                )
            if _DYNAMIC_MENU_PLACEHOLDER_RE.search(combined):
                flags.append(
                    "unresolved dynamic menu loader — rendered/API menu missing"
                )
            if len(sources) == 1 and _is_single_section_url(
                sources[0].get("url") or ""
            ):
                flags.append(
                    "only one menu section captured " f"({sources[0]['url']})"
                )

            # Ordering-platform pages that lazy-load/virtualize their menu
            # leave cart chrome in the text with few actual items — the
            # signature of a PARTIAL capture (found via Chuan Fu / Tamale Co /
            # F&D Cantina, which stored 1-15 items of 76-110-item menus).
            lowered = combined.lower()
            cart_markers = (
                "add to cart",
                "checkout",
                "load more content",
                "view cart",
                "order online",
                "minimum order",
            )
            if prices < 15 and any(marker in lowered for marker in cart_markers):
                flags.append(
                    "looks like a partially captured ordering page "
                    f"({prices} priced items alongside cart/order chrome)"
                )

            # Identical text stored for two different restaurants means a
            # generic chain/platform page, not either restaurant's menu.
            digest = hashlib.sha256(combined.encode()).hexdigest()
            owner = content_owner.get(digest)
            if owner is not None and owner[0] != rid:
                flags.append(f"identical menu text as “{owner[1]}”")
            else:
                content_owner[digest] = (rid, r["name"])

        # Classification producing only a handful of dishes is its own
        # signature: the capture was incomplete but read well enough to be
        # classified and go live. Unclassified restaurants aren't flagged —
        # "never classified" is already visible in the table.
        classified = classified_totals.get(rid, 0)
        if 0 < classified < MIN_PLAUSIBLE_DISHES:
            flags.append(
                f"classified from a small menu — only {classified} "
                f"dish{'es' if classified != 1 else ''} extracted"
            )

        if flags:
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "website_url": r.get("website_url"),
                        "sources": [
                            [source.get("url"), source.get("content")]
                            for source in sources
                        ],
                        "flags": sorted(flags),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            review = reviews.get(rid)
            if review and review.get("fingerprint") != fingerprint:
                review = None
            findings.append(
                {
                    "restaurant_id": rid,
                    "name": r["name"],
                    "flags": flags,
                    "fingerprint": fingerprint,
                    "review_status": review.get("status") if review else None,
                    "review_note": review.get("note") if review else None,
                    "reviewed_at": review.get("reviewed_at") if review else None,
                }
            )

    findings.sort(key=lambda f: len(f["flags"]), reverse=True)
    return findings


def audit_menus(
    db_path: str | None = None, *, force: bool = False
) -> list[dict]:
    """Audit menus, reusing a result only while its DB fingerprint is fresh."""
    path = os.path.abspath(db_path or settings.database_path)
    now = time.monotonic()
    token = _database_token(path)
    with _audit_cache_lock:
        cached = _audit_cache.get(path)
        if (
            not force
            and cached is not None
            and cached[0] == token
            and cached[1] > now
        ):
            return copy.deepcopy(cached[2])

        findings = _audit_menus_uncached(db_path)
        token_after = _database_token(path)
        # Never attach a possibly mixed snapshot to the newer token if a
        # pipeline write landed while the audit was running.
        if token_after == token:
            _audit_cache[path] = (
                token,
                time.monotonic() + AUDIT_CACHE_TTL_SECONDS,
                copy.deepcopy(findings),
            )
        return findings
