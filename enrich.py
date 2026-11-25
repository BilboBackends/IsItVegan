"""Google data: full details for new restaurants, essentials for refreshes.

Google bills each Place Details call at the highest tier of any field in its
mask, and each tier has its own monthly free allowance. So there are two jobs:

- run()     — full enrichment (Enterprise + Atmosphere: 1,000 free a month,
              then $25 per 1,000). Ratings, hours, website, plus
              servesVegetarianFood and an editorialSummary that often names
              signature dishes, which exist even for restaurants whose own
              website we couldn't scrape. Meant for newly added restaurants.
- refresh() — essentials only (Pro: 5,000 free a month, then $17 per 1,000).
              Is it still open, still the same business, still at the same
              address. Meant for the recurring check of known restaurants.

Bulk runs are bounded and resumable: --stale-days skips rows already checked
(rerun the same command to continue), --limit caps the calls, and the
longest-unchecked rows always go first.

We store the booleans/blurb on the restaurant row, and also persist the
editorial summary as a restaurant-level 'text' source so it becomes evidence
the classifier can cite (like scraped menu text).

Runnable in isolation (per CLAUDE.md conventions):

    python enrich.py                    # full details, never-enriched rows
    python enrich.py --stale-days 28    # refresh essentials (resumable)
    python enrich.py --all              # refresh essentials, every active row
    python enrich.py --stale-days 28 --limit 1000 --full   # refetch everything
    python enrich.py --restaurant-id 30 # just one, full details
    python enrich.py --dry-run          # don't write (still makes the calls)
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

import httpx

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db
import google_usage
from config import settings
from location_filter import area_from_address, metro_from_area
from places_client import (
    _RateGate,
    distance_meters,
    fetch_place_details,
    fetch_place_status,
)

# Where the editorial-summary evidence source is recorded as coming from.
_EDITORIAL_URL = "google:editorial_summary"

# Outages hit every row, so give up rather than time out on all of them.
# Restaurant-specific rejections don't count: dead IDs never get a fresh
# timestamp, so they collect at the front of the oldest-first queue and would
# otherwise block every later run.
_MAX_CONSECUTIVE_OUTAGES = 5

# Stay under Google's ~600 requests/minute: a refresh reuses one connection
# and would otherwise go faster than that.
_REFRESH_CALLS_PER_SECOND = 8.0

# What Admin's button runs. The call cap keeps one request under the browser's
# five-minute timeout at the rate above.
REFRESH_STALE_DAYS = 28
REFRESH_MAX_CALLS = 1_500

# Smaller shifts are Google nudging a pin, not a restaurant moving.
_RELOCATED_METERS = 200.0

# Words that say what a place is, not which one. Sharing only these means
# nothing: "Z Cafe Restaurant" became "Ngoc Ha Vietnamese Restaurant".
_GENERIC_NAME_WORDS = frozenset(
    """
    a an and at by de el for in la of on the to with
    bakery bar bbq barbecue bistro bowl bowls brewery brewing buffet burger
    burgers cafe cafeteria catering chicken co coffee company cream cuisine
    deli delivery dessert desserts diner donut donuts eatery eats express fare
    food foods fresh fried garden gourmet grill grille hall house ice inc juice
    kitchen llc lounge market noodle noodles pasta pastries pho pizza pizzeria
    poke pub ramen restaurant restaurante ristorante salad sandwich sandwiches
    seafood shop smoothie smoothies spot steak steakhouse street sub subs sushi
    taco tacos taqueria tavern tea truck wine wings
    american asian caribbean chinese cuban french greek indian italian
    jamaican japanese korean latin mediterranean mexican peruvian thai
    turkish vegan vegetarian venezuelan vietnamese
    downtown east north south west
    """.split()
)

_NEVER = datetime.min.replace(tzinfo=timezone.utc)


def _parse_timestamp(value: str | None) -> datetime | None:
    """Stored ISO timestamp as aware UTC; None when missing or unreadable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _last_enriched(row: dict) -> datetime:
    return _parse_timestamp(row.get("enriched_at")) or _NEVER


def _last_checked(row: dict) -> datetime:
    """Last time Google confirmed the row's status, by either job."""
    checked = _parse_timestamp(row.get("status_checked_at")) or _NEVER
    return max(checked, _last_enriched(row))


def _targets(
    restaurant_id: int | None,
    do_all: bool,
    stale_days: int | None = None,
    last_seen: Callable[[dict], datetime] = _last_enriched,
) -> list[dict]:
    """Pick which restaurants to fetch, longest-unchecked first.

    Bulk modes skip archived rows (a paid call for a listing nobody sees). An
    explicit --restaurant-id still works on any row.
    """
    rows = db.list_restaurants()
    if restaurant_id is not None:
        rows = [r for r in rows if r["id"] == restaurant_id]
        if not rows:
            raise SystemExit(f"No restaurant with id {restaurant_id}.")
        return rows

    rows = [r for r in rows if not r.get("archived")]
    rows.sort(key=last_seen)
    if stale_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
        return [r for r in rows if last_seen(r) < cutoff]
    if do_all:
        return rows
    # Existing databases gain user_rating_count through a migration. NULL
    # means ratings have not been fetched yet, so the next normal enrichment
    # run backfills them once without requiring --all.
    return [
        r
        for r in rows
        if not r.get("enriched_at")
        or r.get("user_rating_count") is None
        or not r.get("hours_enriched_at")
    ]


def _failure_kind(exc: Exception) -> str:
    """'place' (skip this row), 'account' (stop now), or 'outage'."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403, 429):
            return "account"
        # Google reports a bad key as 400, the same status as a bad place ID.
        if status == 400 and "api key" in exc.response.text.lower():
            return "account"
        if status in (400, 404):
            return "place"
    return "outage"


def _failure_text(exc: Exception) -> str:
    """Short reason. Never str(exc): that repeats the full request URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        reason = {
            400: "Google rejected the request",
            401: "Google rejected the API key",
            403: "Google refused access (key, billing, or permissions)",
            404: "Google no longer recognizes this place ID",
            429: "Google quota reached",
        }.get(status, "Google error")
        return f"{reason} (HTTP {status})"
    return type(exc).__name__


@dataclass
class _Tally:
    """What happened to a batch of calls, shared by both jobs."""

    attempted: int = 0
    fetched: int = 0
    failed: list[dict] = field(default_factory=list)
    stopped: str | None = None

    def summary(self, due: int) -> dict:
        return {
            "fetched": self.fetched,
            "failed": self.failed,
            "remaining": due - self.attempted,
            "stopped": self.stopped,
        }

    def report(self, due: int, dry_run: bool) -> None:
        if self.failed:
            print(f"{len(self.failed)} failed (kept as they were, retried next run):")
            for f in self.failed:
                print(f"  - #{f['id']} {f['name']}: {f['error']}")
        if self.stopped:
            print(f"Stopped early: {self.stopped}")
        if due - self.attempted:
            print(f"{due - self.attempted} not reached yet.")
        if dry_run:
            print("[dry-run] Nothing written to the database.")


def _fetch_each(
    targets: list[dict], fetch: Callable[[str], dict], tally: _Tally
) -> Iterator[tuple[dict, dict]]:
    """Yield (restaurant, data) per successful call; one bad row is skipped.

    Only the fetch is guarded. A caller's database error must still end the
    run: carrying on would keep buying data that can't be saved.
    """
    outages_in_a_row = 0
    for t in targets:
        tally.attempted += 1
        try:
            data = fetch(t["place_id"])
        except (httpx.HTTPError, ValueError) as exc:
            reason = _failure_text(exc)
            kind = _failure_kind(exc)
            code = (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else None
            )
            tally.failed.append(
                {"id": t["id"], "name": t["name"], "error": reason, "code": code}
            )
            print(f"  [fail] {t['name']} — {reason}")
            outages_in_a_row = outages_in_a_row + 1 if kind == "outage" else 0
            if kind == "account":
                tally.stopped = reason
                return
            if outages_in_a_row >= _MAX_CONSECUTIVE_OUTAGES:
                tally.stopped = f"{outages_in_a_row} failures in a row ({reason})"
                return
            continue
        outages_in_a_row = 0
        tally.fetched += 1
        yield t, data


def _start(
    verb: str,
    restaurant_id: int | None,
    do_all: bool,
    stale_days: int | None,
    limit: int | None,
    last_seen: Callable[[dict], datetime],
    sku: str,
) -> tuple[list[dict], int]:
    """(rows to fetch, how many are due), after checking the spending bounds."""
    # A negative limit would slice from the end and fetch almost everything.
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1.")
    if stale_days is not None and stale_days < 1:
        raise ValueError("stale_days must be at least 1.")
    if not settings.google_places_api_key:
        raise SystemExit("GOOGLE_PLACES_API_KEY not set in .env.")

    db.init_db()
    eligible = _targets(restaurant_id, do_all, stale_days, last_seen)
    targets = eligible if limit is None else eligible[:limit]
    print(
        f"{verb} {len(targets)} restaurant(s)"
        + (f" of {len(eligible)} due" if len(targets) < len(eligible) else "")
        + "..."
    )
    quote = google_usage.price(sku, len(targets))
    print(
        f"{quote['tier']}: {quote['free_remaining']:,} free calls left this month, "
        f"so up to ${quote['cost']:.2f}.\n"
    )
    return targets, len(eligible)


def refresh_estimate() -> dict:
    """What Admin's closure check would call and cost right now. No API calls."""
    due = len(_targets(None, False, REFRESH_STALE_DAYS, _last_checked))
    calls = min(due, REFRESH_MAX_CALLS)
    return {
        "stale_days": REFRESH_STALE_DAYS,
        "limit": REFRESH_MAX_CALLS,
        "due": due,
        "minutes": round(calls / _REFRESH_CALLS_PER_SECOND / 60, 1),
        **google_usage.price(google_usage.DETAILS_PRO, calls),
    }


def _same_name(a: str, b: str) -> bool:
    """Equal apart from case, spacing, and punctuation."""

    def plain(name: str) -> str:
        return re.sub(r"[\W_]+", "", name.casefold())

    return plain(a) == plain(b)


def _name_words(name: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", name.casefold())
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return set(re.findall(r"\w+", re.sub(r"['\u2018\u2019]s\b", "", plain)))


def _resembles(old: str, new: str, address: str | None) -> bool:
    """Whether a new name reads as the same business, lightly edited.

    The distinctive words of one must all appear in the other; what kind of
    place it is and where it is don't count. Unsure means no: a wrong yes puts
    one restaurant's dishes under another's name.
    """
    old_words, new_words = _name_words(old), _name_words(new)
    ignored = _GENERIC_NAME_WORDS | _name_words(address or "")
    a, b = old_words - ignored, new_words - ignored
    if not a or not b:  # nothing distinctive, so compare everything
        a, b = old_words, new_words
    return a <= b or b <= a


class _History:
    """Keeps what a job found (db.restaurant_changes); nothing on a dry run."""

    def __init__(self, observed_at: str, dry_run: bool, check_id: int | None = None):
        self.observed_at = observed_at
        self.dry_run = dry_run
        self.check_id = check_id
        self._counts: dict[int, dict] | None = None

    def note(
        self, row: dict, change: str, old: str | None = None, new: str | None = None
    ) -> None:
        if self.dry_run:
            return
        if self._counts is None:  # one query, and only when something changed
            self._counts = db.verdict_counts_by_restaurant()
        area = area_from_address(row.get("address"))
        db.record_restaurant_change(
            row["id"],
            change,
            name=row["name"],
            area=area,
            metro=metro_from_area(area),
            observed_at=self.observed_at,
            check_id=self.check_id,
            old_value=old,
            new_value=new,
            vegan_meals=(self._counts.get(row["id"]) or {}).get("vegan_meals", 0),
        )


def refresh(
    do_all: bool = False,
    dry_run: bool = False,
    stale_days: int | None = None,
    limit: int | None = None,
) -> dict:
    """Essentials check of known restaurants (Pro tier). See module docstring.

    Permanent closures are archived and address changes are saved. A new name
    is saved only when it resembles the old one. Otherwise the listing may be
    a different restaurant whose menu we don't have, so it is reported on
    every check until someone deals with it.

    Every check and change is kept; restaurant_history.py reports on them.
    """
    if not do_all and stale_days is None:
        raise ValueError("refresh needs do_all or stale_days.")
    targets, due = _start(
        "Checking", None, do_all, stale_days, limit, _last_checked,
        google_usage.DETAILS_PRO,
    )
    now = datetime.now(timezone.utc).isoformat()
    tally = _Tally()
    closed: list[dict] = []
    moved_listing: list[dict] = []
    temporarily_closed: list[dict] = []
    reopened: list[dict] = []
    renamed: list[dict] = []
    relocated: list[dict] = []
    checked_by_metro: Counter[str] = Counter()
    check_id = db.start_status_check(now) if targets and not dry_run else None
    history = _History(now, dry_run, check_id)

    gate = _RateGate(_REFRESH_CALLS_PER_SECOND)
    client = httpx.Client(timeout=30.0)

    def fetch(place_id: str) -> dict:
        gate.wait()
        return fetch_place_status(
            place_id, api_key=settings.google_places_api_key, client=client
        )

    try:
        for t, found in _fetch_each(targets, fetch, tally):
            row = {"id": t["id"], "name": t["name"]}
            checked_by_metro[
                metro_from_area(area_from_address(t.get("address")))
            ] += 1
            before, after = t.get("business_status"), found["business_status"]
            gone = after == "CLOSED_PERMANENTLY"

            if gone and found["moved_place_id"]:
                moved_listing.append({**row, "place_id": found["moved_place_id"]})
                history.note(t, "moved_listing", new=found["moved_place_id"])
                print(f"  [moved] {t['name']} → listing {found['moved_place_id']}")
            elif gone:
                closed.append(row)
                history.note(t, "closed", old=before)
                print(f"  [closed] {t['name']}")
            elif after == "CLOSED_TEMPORARILY" and before != after:
                temporarily_closed.append(row)
                history.note(t, "temporarily_closed")
                print(f"  [temporarily closed] {t['name']}")
            elif after == "OPERATIONAL" and before == "CLOSED_TEMPORARILY":
                reopened.append(row)
                history.note(t, "reopened")
                print(f"  [reopened] {t['name']}")

            new_name = found["name"]
            if new_name and _same_name(new_name, t["name"]):
                new_name = None
            review = bool(new_name) and not _resembles(
                t["name"], new_name, t.get("address")
            )
            # A closed listing is leaving anyway; its name no longer matters.
            if new_name and not gone:
                renamed.append({**row, "new_name": new_name, "review": review})
                kind = "name_review" if review else "renamed"
                # A flagged name comes back every check; it happened once.
                if not db.has_restaurant_change(t["id"], kind, new_value=new_name):
                    history.note(t, kind, old=t["name"], new=new_name)
                label = "check name" if review else "renamed"
                print(f"  [{label}] {t['name']} → {new_name}")

            coords = (t.get("lat"), t.get("lng"), found["lat"], found["lng"])
            if not gone and None not in coords:
                meters = distance_meters(*coords)
                if meters > _RELOCATED_METERS:
                    relocated.append(
                        {**row, "meters": round(meters), "address": found["address"]}
                    )
                    history.note(
                        t, "relocated", old=t.get("address"), new=found["address"]
                    )
                    print(f"  [address moved] {t['name']} → {found['address']}")

            if not dry_run:
                db.update_place_status(
                    t["id"],
                    checked_at=now,
                    business_status=after,
                    name=None if review or gone else new_name,
                    address=found["address"],
                    lat=found["lat"],
                    lng=found["lng"],
                )
                # Data is kept; Admin's Archived tab still shows it.
                if gone:
                    db.set_restaurant_archived(t["id"], True)

        by_id = {t["id"]: t for t in targets}
        for f in tally.failed:
            if f["code"] != 404:
                continue
            # Once per disappearance, not once per check that can't find it.
            t = by_id[f["id"]]
            since = _last_checked(t).isoformat()
            if not db.has_restaurant_change(t["id"], "missing", since=since):
                history.note(t, "missing")
    finally:
        client.close()
        if check_id is not None:
            db.finish_status_check(
                check_id,
                checked=tally.fetched,
                failed=len(tally.failed),
                stopped=tally.stopped,
                checked_by_metro=checked_by_metro,
            )

    print(
        f"\nDone. {tally.fetched} checked: {len(closed)} closed, "
        f"{len(moved_listing)} moved to a new listing, "
        f"{len(temporarily_closed)} temporarily closed, {len(reopened)} reopened, "
        f"{len(renamed)} renamed ({sum(r['review'] for r in renamed)} to check), "
        f"{len(relocated)} with a new address."
    )
    tally.report(due, dry_run)
    return {
        "closed": closed,
        "moved_listing": moved_listing,
        "temporarily_closed": temporarily_closed,
        "reopened": reopened,
        "renamed": renamed,
        "relocated": relocated,
        **tally.summary(due),
    }


def run(
    restaurant_id: int | None = None,
    do_all: bool = False,
    dry_run: bool = False,
    stale_days: int | None = None,
    limit: int | None = None,
) -> dict:
    """Full enrichment (Enterprise + Atmosphere). See module docstring.

    stale_days targets only rows not enriched within that many days, so
    rerunning the same command continues where the last one ended. limit caps
    the calls made.
    """
    targets, due = _start(
        "Enriching", restaurant_id, do_all, stale_days, limit, _last_enriched,
        google_usage.DETAILS_ATMOSPHERE,
    )
    now = datetime.now(timezone.utc).isoformat()
    veg_yes = veg_no = veg_unknown = with_editorial = with_rating = 0
    tally = _Tally()
    history = _History(now, dry_run)

    def fetch(place_id: str) -> dict:
        return fetch_place_details(
            place_id, api_key=settings.google_places_api_key
        )

    for t, details in _fetch_each(targets, fetch, tally):
        veg = details["serves_vegetarian"]
        veg_str = {True: "veg✓", False: "veg✗", None: "veg?"}[veg]
        if veg is True:
            veg_yes += 1
        elif veg is False:
            veg_no += 1
        else:
            veg_unknown += 1

        editorial = details["editorial_summary"]
        if editorial:
            with_editorial += 1
        if details["rating"] is not None:
            with_rating += 1

        print(f"  [{veg_str}] {t['name']}  ({details['primary_type'] or '—'})")
        if editorial:
            print(f"          “{editorial[:70]}”")

        if not dry_run:
            db.update_food_signals(
                t["id"],
                serves_vegetarian=veg,
                price_level=details["price_level"],
                primary_type=details["primary_type"],
                editorial_summary=editorial,
                rating=details["rating"],
                user_rating_count=details["user_rating_count"],
                open_now=details["open_now"],
                opening_hours=details["opening_hours"],
                enriched_at=now,
                business_status=details.get("business_status"),
                website_url=details.get("website_url"),
            )
            # Google says it's gone for good — retire the listing so it
            # leaves consumer views and pipeline runs (data kept; Admin's
            # Archived tab still shows it).
            if details.get("business_status") == "CLOSED_PERMANENTLY":
                if t.get("business_status") != "CLOSED_PERMANENTLY":
                    history.note(t, "closed", old=t.get("business_status"))
                db.set_restaurant_archived(t["id"], True)
                print("          ⚠ CLOSED PERMANENTLY — auto-archived")
            # Persist the blurb as citable evidence alongside scraped menu text.
            if editorial:
                db.upsert_menu_text(
                    restaurant_id=t["id"],
                    url=_EDITORIAL_URL,
                    content=f"Google editorial summary: {editorial}",
                    fetched_at=now,
                )

    print(
        f"\nDone. {tally.fetched} enriched. "
        f"vegetarian: {veg_yes} yes / {veg_no} no / {veg_unknown} unknown. "
        f"{with_editorial} have an editorial summary; {with_rating} have ratings."
    )
    tally.report(due, dry_run)
    return {
        "veg_yes": veg_yes,
        "veg_no": veg_no,
        "veg_unknown": veg_unknown,
        "with_editorial": with_editorial,
        "with_rating": with_rating,
        **tally.summary(due),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Google restaurant data.")
    parser.add_argument("--restaurant-id", type=int, default=None)
    parser.add_argument(
        "--all", action="store_true",
        help="Refresh essentials for every active restaurant, even if just "
        "checked.",
    )
    parser.add_argument(
        "--stale-days", type=int, default=None,
        help="Refresh essentials for restaurants not checked in this many "
        "days. Rerun the same command to continue an unfinished refresh.",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="With --all or --stale-days: refetch ratings, hours, and food "
        "signals too. Enterprise + Atmosphere tier, 1,000 free a month "
        "(essentials are Pro, 5,000 free).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Make at most this many Google calls.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Don't write results. The Google calls are still made and billed.",
    )
    args = parser.parse_args()
    bulk = args.all or args.stale_days is not None
    options = {
        "do_all": args.all,
        "dry_run": args.dry_run,
        "stale_days": args.stale_days,
        "limit": args.limit,
    }
    try:
        if bulk and not args.full and args.restaurant_id is None:
            refresh(**options)
        else:
            run(restaurant_id=args.restaurant_id, **options)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
