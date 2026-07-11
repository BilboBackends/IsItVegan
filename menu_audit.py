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

import hashlib
import json
import re

import db
from menu_score import score_menu_text
from venue_filter import is_consumer_food_venue

# Below this, a stored "menu" is almost certainly a teaser or a fragment
# (matches the scraper's own confidence floor).
MIN_PLAUSIBLE_CHARS = 1200

# A stored menu whose score sits barely above the keep threshold is usually
# marketing copy that squeaked past on section names (the Pickles case).
WEAK_SCORE = 0.60

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


def _is_single_section_url(url: str) -> bool:
    from urllib.parse import urlparse

    path = urlparse(url or "").path.lower()
    return any(
        re.search(rf"(?<![a-z]){w}(?![a-z])", path) for w in _SECTION_WORDS
    )


def _menu_sources(conn, restaurant_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, url, content, fetched_at FROM sources
        WHERE restaurant_id = ? AND type = 'text'
          AND (url IS NULL OR url != 'google:editorial_summary')
        ORDER BY id ASC
        """,
        (restaurant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def audit_menus(db_path: str | None = None) -> list[dict]:
    """Audit every consumer-facing restaurant's stored menu text.

    Returns [{restaurant_id, name, flags: [str, ...]}, ...] for restaurants
    with at least one flag, worst-first (most flags first).
    """
    restaurants = [
        r for r in db.list_restaurants(db_path) if is_consumer_food_venue(r)
    ]
    reviews = db.list_menu_quality_reviews(db_path)

    with db.connect(db_path) as conn:
        per_restaurant: dict[int, dict] = {}
        content_owner: dict[str, tuple[int, str]] = {}  # hash -> (id, name)
        findings: list[dict] = []

        for r in restaurants:
            sources = _menu_sources(conn, r["id"])
            per_restaurant[r["id"]] = {"restaurant": r, "sources": sources}

        for rid, bundle in per_restaurant.items():
            r = bundle["restaurant"]
            sources = bundle["sources"]
            flags: list[str] = []

            if not sources:
                if r.get("website_url"):
                    flags.append("website exists but no menu scraped")
                # No website and no menu: photo-fallback case, not a scraper
                # regression — don't flag.
            else:
                combined = "\n".join(s["content"] or "" for s in sources)
                score = score_menu_text(combined)
                prices = len(_PRICE_RE.findall(combined))

                if len(combined) < MIN_PLAUSIBLE_CHARS:
                    flags.append(
                        f"menu suspiciously small ({len(combined)} chars)"
                    )
                if prices == 0:
                    flags.append("no prices anywhere in menu text")
                if score.score < WEAK_SCORE:
                    flags.append(
                        f"weak menu score ({score.score:.2f}) — may be "
                        "marketing copy, not a menu"
                    )
                if _DYNAMIC_MENU_PLACEHOLDER_RE.search(combined):
                    flags.append(
                        "unresolved dynamic menu loader — rendered/API menu missing"
                    )
                if (
                    len(sources) == 1
                    and _is_single_section_url(sources[0].get("url") or "")
                ):
                    flags.append(
                        "only one menu section captured "
                        f"({sources[0]['url']})"
                    )

                # Ordering-platform pages that lazy-load/virtualize their
                # menu leave cart chrome in the text with few actual items —
                # the signature of a PARTIAL capture (found via Chuan Fu /
                # Tamale Co / F&D Cantina, which stored 1-15 items of
                # 76-110-item menus).
                lowered = combined.lower()
                cart_markers = (
                    "add to cart", "checkout", "load more content",
                    "view cart", "order online", "minimum order",
                )
                if prices < 15 and any(m in lowered for m in cart_markers):
                    flags.append(
                        f"looks like a partially captured ordering page "
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
