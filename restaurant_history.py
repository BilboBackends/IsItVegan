"""What closure checks have found over time, grouped by month.

enrich.refresh records each check (status_checks, the denominator) and every
change it finds (restaurant_changes). Monthly because that is the check's
rhythm: free Google calls reset monthly, and a check resumed over several runs
is one check to the reader.

    python restaurant_history.py
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db

# Both take the restaurant off the site.
_GONE = ("closed", "moved_listing")


def _month(timestamp: str) -> str:
    return datetime.fromisoformat(timestamp).astimezone().strftime("%Y-%m")


def _rate(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def report() -> dict:
    """Per-month numbers and the changes behind them, newest month first."""
    months: dict[str, dict] = {}

    def month(timestamp: str) -> dict:
        key = _month(timestamp)
        return months.setdefault(
            key,
            {"month": key, "checked": 0, "failed": 0, "counts": Counter(),
             "checked_by_metro": Counter(), "closed_by_metro": Counter(),
             "vegan_meals_lost": 0, "changes": []},
        )

    for check in db.list_status_checks():
        entry = month(check["started_at"])
        entry["checked"] += check["checked"]
        entry["failed"] += check["failed"]
        entry["checked_by_metro"].update(check["checked_by_metro"])
    for change in db.list_restaurant_changes():
        entry = month(change["observed_at"])
        entry["counts"][change["change_type"]] += 1
        entry["changes"].append(change)
        if change["change_type"] in _GONE:
            entry["vegan_meals_lost"] += change["vegan_meals"]
            entry["closed_by_metro"][change["metro"]] += 1

    out = []
    for key in sorted(months, reverse=True):
        entry = months[key]
        checked = entry.pop("checked_by_metro")
        closed = entry.pop("closed_by_metro")
        entry["counts"] = dict(entry["counts"])
        entry["closure_rate"] = _rate(
            sum(entry["counts"].get(kind, 0) for kind in _GONE), entry["checked"]
        )
        entry["metros"] = [
            {"metro": metro, "checked": checked[metro], "closed": closed[metro],
             "rate": _rate(closed[metro], checked[metro])}
            for metro in sorted(set(checked) | set(closed), key=lambda m: -checked[m])
        ]
        out.append(entry)
    return {"months": out}


def main() -> None:
    for entry in report()["months"]:
        print(f"{entry['month']}: {entry['checked']:,} checked")
        for kind, count in sorted(entry["counts"].items(), key=lambda kv: -kv[1]):
            print(f"  {kind:<20} {count}")
        print(f"  closure rate {entry['closure_rate']}%, "
              f"{entry['vegan_meals_lost']} vegan meals lost")
        for row in entry["metros"]:
            print(f"    {row['metro']}: {row['closed']} of {row['checked']} "
                  f"({row['rate']}%)")


if __name__ == "__main__":
    main()
