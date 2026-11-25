"""Monthly Google Places usage: calls made, free calls left, cost past them.

Google has no usage endpoint an API key can read, so this counts the calls the
app makes. places_client records each one after it succeeds, because Google
bills successful requests only. Calls made elsewhere with the same key are not
seen; Google's own total is at https://console.cloud.google.com/billing/reports.
--set corrects a count to match it.

Every SKU has its own free allowance, which resets on the 1st at midnight
Pacific. IDs-only searches are unlimited and free, so they aren't counted.

    python google_usage.py                         # this month so far
    python google_usage.py --set details_pro 120   # match Google's own count
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

import db
from config import settings

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class Sku:
    id: str
    label: str
    free: int  # successful calls per month
    price_per_1000: float  # first paid band (up to 100,000 calls a month)
    used_for: str


DETAILS_PRO = "details_pro"
DETAILS_ATMOSPHERE = "details_atmosphere"
NEARBY_PRO = "nearby_pro"
TEXT_PRO = "text_pro"
PHOTOS = "photos"

# developers.google.com/maps/billing-and-pricing/pricing, checked 2026-11-25.
SKUS: dict[str, Sku] = {
    sku.id: sku
    for sku in (
        Sku(DETAILS_PRO, "Place Details Pro", 5_000, 17.0,
            "closure checks, sweep lookups"),
        Sku(DETAILS_ATMOSPHERE, "Place Details Enterprise + Atmosphere", 1_000,
            25.0, "full details for new restaurants"),
        Sku(NEARBY_PRO, "Nearby Search Pro", 5_000, 32.0,
            "discovery, audit sweeps"),
        Sku(TEXT_PRO, "Text Search Pro", 5_000, 32.0,
            "adding by name, area prospecting"),
        Sku(PHOTOS, "Place Details Photos", 1_000, 7.0,
            "menu-photo fallback"),
    )
}

# Calls counted but not yet stored, kept when the database is busy.
_pending: dict[tuple[str, str], int] = {}
_lock = threading.RLock()
_ready: set[str] = set()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _boundary(year: int, month: int) -> datetime:
    """Midnight Pacific on the 1st, in UTC.

    On the 1st, daylight time is in effect from April through November (the
    changes fall later in March, and no earlier than 2am on November 1).
    """
    return datetime(year, month, 1, 7 if 4 <= month <= 11 else 8, tzinfo=timezone.utc)


def _period(at: datetime | None = None) -> tuple[str, datetime, datetime]:
    """(billing month, its start, its end) for a moment."""
    moment = (at or _utcnow()).astimezone(timezone.utc)
    year, month = moment.year, moment.month
    if moment < _boundary(year, month):  # still last month in Pacific time
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    following = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year:04d}-{month:02d}", _boundary(year, month), _boundary(*following)


def _connect():
    path = settings.database_path
    if path not in _ready:
        db.init_db(path)
        with db.connect(path) as conn:
            # When counting began, so a partial month can say so.
            conn.execute(
                "INSERT OR IGNORE INTO google_api_usage (month, sku, calls, first_at)"
                " VALUES ('', 'started', 0, ?)",
                (_utcnow().isoformat(),),
            )
        _ready.add(path)
    return db.connect(path)


def _flush() -> None:
    with _connect() as conn:
        for (month, sku), calls in _pending.items():
            conn.execute(
                """
                INSERT INTO google_api_usage (month, sku, calls, first_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(month, sku) DO UPDATE SET
                    calls = google_api_usage.calls + excluded.calls
                """,
                (month, sku, calls, _utcnow().isoformat()),
            )
    _pending.clear()


def record(sku: str, calls: int = 1) -> None:
    """Count successful calls.

    Never raises: the call is already made, and losing its result over a busy
    database would be worse. An unsaved count goes in with the next one.
    """
    with _lock:
        key = (_period()[0], SKUS[sku].id)
        _pending[key] = _pending.get(key, 0) + calls
        try:
            _flush()
        except sqlite3.Error as exc:
            print(f"  [google usage] count not saved yet: {exc}")


def set_count(sku: str, calls: int) -> None:
    """Overwrite this month's count, to match Google's billing report."""
    if sku not in SKUS:
        raise ValueError(f"Unknown SKU {sku!r}. Use one of: {', '.join(SKUS)}.")
    if calls < 0:
        raise ValueError("calls can't be negative.")
    month = _period()[0]
    with _lock:
        _pending.pop((month, sku), None)
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO google_api_usage (month, sku, calls, first_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(month, sku) DO UPDATE SET calls = excluded.calls
                """,
                (month, sku, calls, _utcnow().isoformat()),
            )


def _used(month: str) -> dict[str, int]:
    with _lock:
        with _connect() as conn:
            used = {
                row["sku"]: row["calls"]
                for row in conn.execute(
                    "SELECT sku, calls FROM google_api_usage WHERE month = ?",
                    (month,),
                )
            }
        for (pending_month, sku), calls in _pending.items():
            if pending_month == month:
                used[sku] = used.get(sku, 0) + calls
    return used


def usage() -> dict:
    """This month's count, free calls left, and price for every SKU."""
    month, starts, resets = _period()
    used = _used(month)
    with _connect() as conn:
        started = conn.execute(
            "SELECT first_at FROM google_api_usage WHERE month = ''"
        ).fetchone()["first_at"]
    partial = datetime.fromisoformat(started) > starts
    return {
        "month": month,
        "resets_at": resets.isoformat(),
        # Set when this month began before counting did.
        "counting_since": started if partial else None,
        "skus": [
            {
                "id": sku.id,
                "label": sku.label,
                "used_for": sku.used_for,
                "free": sku.free,
                "used": used.get(sku.id, 0),
                "remaining": max(0, sku.free - used.get(sku.id, 0)),
                "over": max(0, used.get(sku.id, 0) - sku.free),
                "price_per_1000": sku.price_per_1000,
                "spent": round(
                    max(0, used.get(sku.id, 0) - sku.free)
                    * sku.price_per_1000 / 1000, 2
                ),
            }
            for sku in SKUS.values()
        ],
    }


def price(sku_id: str, calls: int) -> dict:
    """What a batch of calls would cost, given the free calls left."""
    sku = SKUS[sku_id]
    remaining = max(0, sku.free - _used(_period()[0]).get(sku_id, 0))
    billable = max(0, calls - remaining)
    return {
        "sku": sku.id,
        "tier": sku.label,
        "calls": calls,
        "free_remaining": remaining,
        "billable": billable,
        "price_per_1000": sku.price_per_1000,
        "cost": round(billable * sku.price_per_1000 / 1000, 2),
        "cost_without_free": round(calls * sku.price_per_1000 / 1000, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Google Places usage this month.")
    parser.add_argument(
        "--set", nargs=2, metavar=("SKU", "CALLS"),
        help=f"Set this month's count for one of: {', '.join(SKUS)}.",
    )
    args = parser.parse_args()
    if args.set:
        try:
            set_count(args.set[0], int(args.set[1]))
        except ValueError as exc:
            parser.error(str(exc))
    report = usage()
    print(f"Google Places usage for {report['month']} (resets {report['resets_at']})")
    for row in report["skus"]:
        print(
            f"  {row['id']:<19} {row['used']:>6,} of {row['free']:,} free used, "
            f"{row['remaining']:,} left, then ${row['price_per_1000']:g} per 1,000"
            + (f"  (${row['spent']:.2f} so far)" if row["over"] else "")
        )
    if report["counting_since"]:
        print(f"  Counting began {report['counting_since'][:10]}; "
              "earlier calls this month aren't included.")


if __name__ == "__main__":
    main()
