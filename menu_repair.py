"""Bounded audit -> rescrape -> re-audit loop for incomplete menu captures.

The quality audit contains both human-review findings and machine-repairable
scraper failures. This module retries only the latter, then fingerprints the
next audit pass. It stops when findings clear, when evidence stops changing,
or at the pass limit, so a stubborn site cannot loop forever.
"""
from __future__ import annotations

import argparse
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import ingest
import menu_audit


_AUTO_REPAIR_FLAG_MARKERS = (
    "website exists but no menu scraped",
    "menu suspiciously small",
    "weak menu score",
    "only one menu section captured",
    "partially captured ordering page",
    "unresolved dynamic menu loader",
)


def auto_repair_findings(
    findings: list[dict], restaurant_ids: set[int] | None = None
) -> list[dict]:
    """Active audit findings whose evidence describes a scraper failure."""
    return [
        finding
        for finding in findings
        if not finding.get("review_status")
        and (restaurant_ids is None or finding["restaurant_id"] in restaurant_ids)
        and any(
            marker in flag.lower()
            for flag in finding.get("flags", [])
            for marker in _AUTO_REPAIR_FLAG_MARKERS
        )
    ]


def run(
    *,
    max_passes: int = 2,
    restaurant_ids: list[int] | None = None,
    dry_run: bool = False,
) -> dict:
    """Repair incomplete menus, recursively re-auditing after each pass."""
    if max_passes < 1 or max_passes > 5:
        raise ValueError("max_passes must be between 1 and 5")
    requested = set(restaurant_ids) if restaurant_ids else None
    findings = menu_audit.audit_menus()
    initial = auto_repair_findings(findings, requested)
    passes: list[dict] = []
    attempted_ids: set[int] = set()

    for pass_number in range(1, max_passes + 1):
        candidates = auto_repair_findings(findings, requested)
        if not candidates:
            break
        ids = [finding["restaurant_id"] for finding in candidates]
        before = {finding["restaurant_id"]: finding["fingerprint"] for finding in candidates}
        print(
            f"Repair pass {pass_number}: {len(ids)} restaurant(s) — "
            + ", ".join(finding["name"] for finding in candidates)
        )
        if dry_run:
            passes.append({"pass": pass_number, "restaurant_ids": ids, "dry_run": True})
            break

        attempted_ids.update(ids)
        ingest_summary = ingest.run(restaurant_ids=ids)
        findings = menu_audit.audit_menus()
        remaining = {
            finding["restaurant_id"]: finding["fingerprint"]
            for finding in auto_repair_findings(findings, requested)
        }
        changed = sorted(
            restaurant_id
            for restaurant_id in ids
            if remaining.get(restaurant_id) != before.get(restaurant_id)
        )
        passes.append(
            {
                "pass": pass_number,
                "restaurant_ids": ids,
                "succeeded": ingest_summary["succeeded"],
                "failed": ingest_summary["failed"],
                "changed_fingerprints": changed,
            }
        )
        if not changed:
            print("No audit fingerprints changed; stopping to avoid repeated retries.")
            break

    remaining_findings = auto_repair_findings(findings, requested)
    remaining_ids = {finding["restaurant_id"] for finding in remaining_findings}
    repaired_ids = sorted(
        finding["restaurant_id"]
        for finding in initial
        if finding["restaurant_id"] not in remaining_ids
    )
    return {
        "initial": len(initial),
        "passes": passes,
        "attempted_restaurant_ids": sorted(attempted_ids),
        "repaired_restaurant_ids": repaired_ids,
        "remaining": len(remaining_findings),
        "remaining_findings": remaining_findings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recursively audit and repair incomplete menu scrapes."
    )
    parser.add_argument("--max-passes", type=int, default=2)
    parser.add_argument("--restaurant-id", type=int, action="append", dest="ids")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    summary = run(
        max_passes=args.max_passes,
        restaurant_ids=args.ids,
        dry_run=args.dry_run,
    )
    print(
        f"Repair complete: {len(summary['repaired_restaurant_ids'])} fixed, "
        f"{summary['remaining']} still need review."
    )
    if summary["repaired_restaurant_ids"]:
        print(
            "Reclassify repaired restaurant IDs: "
            + ", ".join(str(value) for value in summary["repaired_restaurant_ids"])
        )


if __name__ == "__main__":
    main()
