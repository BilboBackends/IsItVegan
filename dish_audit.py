"""Tier-0 dish audits: deterministic sanity checks over already-stored data.

The classification pipeline stores each dish's price, calories, verdict, and
dietary attributes. Some of those are wrong in ways no model call is needed
to catch — the wrongness is visible in the numbers themselves:

- a `vegan` verdict on a dish the SAME model tagged `dairy_status: contains`
  ("Guacamole" vegan but contains dairy; "Carrot Sticks & Ranch" likely_vegan
  but contains dairy),
- a 2000-calorie item tagged as a `side`,
- a "$99 Dinner Rolls" whose price is really $0.99 — the decimal was dropped
  during extraction.

Every check here is pure logic over the DB — zero LLM tokens — so the whole
suite can sweep every restaurant on demand. A finding is a *prompt to look*,
never an automatic verdict (matching menu_audit.py's philosophy). The one
exception is the lost-decimal price case, which carries a `suggested`
correction the Admin can apply in one click, because restoring a dropped
decimal is unambiguous.

Calibration note (see the price-calorie-audit memory): real menus contain
$900 whisky, 2840-calorie wings, and $275 catering platters, so magnitude
alone means almost nothing — ~90% of "expensive" items are legitimately
expensive. General price re-derivation from the flattened menu blob is
unreliable (it grabs a neighbor's number), so this module deliberately does
NOT flag prices by size or by text re-derivation — only the unambiguous
lost-decimal shape. Catching a genuinely mis-parsed price (a $100 burger)
belongs at extraction time, where the model still sees the line item.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import db

# Pull every number out of a free-text price/calorie string. Prices arrive as
# 'from $10.99', '$99–$149', '$110/lb', '8oz 4.25; 12oz 5.50', bare '90';
# calories as 'cal 580', '1000-cal 1990', '2220 cal'. We keep the whole set
# so a multi-size string's spread is visible, not just its max.
_NUMBER_RE = re.compile(r"\d{1,4}(?:\.\d{1,2})?")

# The only price shape we flag: a cents-only string ('.99', '+.79') stored
# as whole dollars because the decimal point was dropped during extraction.
# Everything else about prices is left to extraction-time validation — see
# the module docstring for why post-hoc magnitude/re-derivation checks are
# unreliable on real menu data.
_CENTS_ONLY_RE = re.compile(r"^\s*\+?\.(\d{2})\b")

# Calories are bounded by human physiology, not menu economics: even a
# 2840-cal platter is real, but a single item over this is almost always a
# parse error (two items' numbers fused). Sides/desserts get a lower bar.
_CALORIE_CEILING_MEAL = 3200
_CALORIE_CEILING_SIDE = 1500
_CALORIE_FLOOR = 5  # a "5 cal" entree is a misparse; 0/blank is just missing


@dataclass
class Finding:
    """One audit hit. `suggested` is set only when the fix is unambiguous
    (a lost-decimal price)."""

    restaurant_id: int
    dish_id: int
    dish_name: str
    code: str          # stable machine key, e.g. "calorie_high"
    severity: str      # "high" | "medium" | "low"
    message: str       # human sentence
    field_name: str | None = None   # dish column the finding is about
    current: str | None = None       # the stored value in question
    suggested: str | None = None     # unambiguous replacement, if any
    evidence: str | None = None       # supporting text

    def fingerprint(self) -> str:
        """Stable identity of the flagged VALUE, so a dismissal re-appears if
        the underlying data later changes."""
        import hashlib

        payload = f"{self.code}|{self.field_name}|{self.current}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def numbers_in(text: str | None) -> list[float]:
    """Every numeric token in a price/calorie string, in order."""
    if not text:
        return []
    return [float(m) for m in _NUMBER_RE.findall(text)]


def price_value(text: str | None) -> float | None:
    """A single representative price for cohort comparison.

    Uses the SMALLEST number in a multi-size/range string ('$99–$149',
    '75 / 150', '8oz 4.25; 12oz 5.50') — the base/entry price is the fair
    thing to compare against other dishes, and it also resists a bled-in
    large catering number inflating the dish's apparent price.
    """
    nums = [n for n in numbers_in(text) if n > 0]
    return min(nums) if nums else None


def calorie_value(text: str | None) -> float | None:
    """Representative calorie count — the MAX, since a range like
    '1000-cal 1990' means the dish can reach the top figure."""
    nums = [n for n in numbers_in(text) if n > 0]
    return max(nums) if nums else None


def _format_price(value: float) -> str:
    if value == int(value):
        return f"${int(value)}"
    return f"${value:.2f}"


def _audit_prices(dishes: list[dict]) -> list[Finding]:
    """Flag the one unambiguous price misparse: a lost decimal.

    'Dinner Rolls .99' stored as $99. The fix (restore the dropped decimal)
    is deterministic, so this finding carries a one-click correction.
    """
    findings: list[Finding] = []
    for d in dishes:
        raw = d.get("price")
        v = price_value(raw)
        if v is None:
            continue
        cents = _CENTS_ONLY_RE.match(raw)
        if cents and v >= 10:
            corrected = f"$0.{cents.group(1)}"
            findings.append(
                Finding(
                    restaurant_id=d["restaurant_id"],
                    dish_id=d["id"],
                    dish_name=d["name"],
                    code="price_lost_decimal",
                    severity="high",
                    message=(
                        f"Price {raw!r} for “{d['name']}” lost its decimal — "
                        f"looks like {corrected}, not {_format_price(v)}"
                    ),
                    field_name="price",
                    current=raw,
                    suggested=corrected,
                    evidence="cents-only price string stored as whole dollars",
                )
            )
    return findings


def _audit_calories(dishes: list[dict]) -> list[Finding]:
    findings: list[Finding] = []
    for d in dishes:
        raw = d.get("calories")
        v = calorie_value(raw)
        if v is None:
            continue
        role = d.get("serving_role") or "unclear"
        ceiling = _CALORIE_CEILING_SIDE if role == "side" else _CALORIE_CEILING_MEAL
        if v > ceiling:
            findings.append(
                Finding(
                    restaurant_id=d["restaurant_id"],
                    dish_id=d["id"],
                    dish_name=d["name"],
                    code="calorie_high",
                    severity="medium",
                    message=(
                        f"{int(v)} cal is implausibly high for a {role} "
                        f"(“{d['name']}”) — likely a number parsed from an "
                        "adjacent item"
                    ),
                    field_name="calories",
                    current=raw,
                    evidence=f"ceiling for {role} is {ceiling} cal",
                )
            )
        elif v < _CALORIE_FLOOR:
            findings.append(
                Finding(
                    restaurant_id=d["restaurant_id"],
                    dish_id=d["id"],
                    dish_name=d["name"],
                    code="calorie_low",
                    severity="low",
                    message=f"{int(v)} cal is implausibly low for “{d['name']}”",
                    field_name="calories",
                    current=raw,
                )
            )
    return findings


# Verdict/attribute contradictions the SAME classification asserts. A model
# that says "vegan" and "contains dairy" in one row is signalling low
# confidence in its own output — a strong, free misclassification prompt.
_VEGANISH = {"vegan", "likely_vegan"}


def _audit_consistency(dishes: list[dict]) -> list[Finding]:
    findings: list[Finding] = []
    for d in dishes:
        verdict = d.get("verdict")
        if verdict is None:
            continue

        def flag(code: str, msg: str, sev: str = "high") -> None:
            findings.append(
                Finding(
                    restaurant_id=d["restaurant_id"],
                    dish_id=d["id"],
                    dish_name=d["name"],
                    code=code,
                    severity=sev,
                    message=msg,
                )
            )

        if verdict in _VEGANISH and d.get("dairy_status") == "contains":
            flag(
                "verdict_vs_dairy",
                f"“{d['name']}” is {verdict} but its own dairy_status is "
                "'contains' — one of the two is wrong",
            )
        # A vegan dish can't contain egg/honey; those live in nut/dairy-free
        # space but are caught via the animal-word guardrail, so here we only
        # assert the dairy contradiction, which is the common bleed.
        if verdict == "not_vegan" and d.get("dairy_status") == "free":
            # Not necessarily wrong (meat with no dairy is dairy-free and
            # not_vegan), so low severity — informational, easy to dismiss.
            pass
    return findings


def audit_restaurant(dishes: list[dict]) -> list[Finding]:
    """All Tier-0 findings for one restaurant's dishes."""
    findings: list[Finding] = []
    findings += _audit_prices(dishes)
    findings += _audit_calories(dishes)
    findings += _audit_consistency(dishes)
    return findings


def audit_all(db_path: str | None = None) -> list[Finding]:
    """Sweep every active restaurant's dishes for Tier-0 field problems.

    Returns findings worst-first (high severity, then by restaurant). Pure
    reads; no tokens; safe to run on demand across the whole DB.
    """
    from venue_filter import is_consumer_food_venue

    restaurants = [
        r for r in db.list_restaurants(db_path)
        if is_consumer_food_venue(r) and not r.get("archived")
    ]
    by_id = {r["id"]: r for r in restaurants}
    findings: list[Finding] = []
    for r in restaurants:
        dishes = db.list_dishes(r["id"], db_path=db_path)
        if not dishes:
            continue
        for d in dishes:
            d.setdefault("restaurant_id", r["id"])
        findings += audit_restaurant(dishes)

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (severity_rank.get(f.severity, 3), f.restaurant_id))
    return findings


def findings_as_dicts(
    findings: list[Finding],
    *,
    include_dismissed: bool = False,
    db_path: str | None = None,
) -> list[dict]:
    """Findings enriched with restaurant name, for the API/Admin panel.

    Dismissed findings are dropped unless include_dismissed; a dismissal only
    still applies while the flagged value is unchanged (matched fingerprint).
    """
    names = {r["id"]: r["name"] for r in db.list_restaurants(db_path)}
    reviews = db.list_dish_audit_reviews(db_path)
    out = []
    for f in findings:
        review = reviews.get((f.dish_id, f.code))
        dismissed = bool(review and review.get("fingerprint") == f.fingerprint())
        if dismissed and not include_dismissed:
            continue
        out.append(
            {
                "restaurant_id": f.restaurant_id,
                "restaurant_name": names.get(f.restaurant_id),
                "dish_id": f.dish_id,
                "dish_name": f.dish_name,
                "code": f.code,
                "severity": f.severity,
                "message": f.message,
                "field": f.field_name,
                "current": f.current,
                "suggested": f.suggested,
                "evidence": f.evidence,
                "fingerprint": f.fingerprint(),
                "dismissed": dismissed,
            }
        )
    return out


def main() -> None:
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Tier-0 dish audits (price/calorie/consistency). No tokens."
    )
    parser.add_argument("--code", help="Only show findings with this code.")
    args = parser.parse_args()

    db.init_db()
    findings = audit_all()
    if args.code:
        findings = [f for f in findings if f.code == args.code]

    from collections import Counter

    by_code = Counter(f.code for f in findings)
    print(f"{len(findings)} finding(s): {dict(by_code)}\n")
    for f in findings:
        line = f"  [{f.severity}] {f.code}: {f.message}"
        if f.suggested:
            line += f"  (suggested: {f.suggested})"
        print(line)


if __name__ == "__main__":
    main()
