from __future__ import annotations

import menu_repair


def _finding(fingerprint: str) -> dict:
    return {
        "restaurant_id": 7,
        "name": "Dynamic Cafe",
        "flags": ["unresolved dynamic menu loader — rendered/API menu missing"],
        "fingerprint": fingerprint,
        "review_status": None,
    }


def test_repair_loop_reaudits_until_finding_clears(monkeypatch):
    audits = [[_finding("first")], [_finding("second")], []]
    ingest_calls = []
    monkeypatch.setattr(menu_repair.menu_audit, "audit_menus", lambda: audits.pop(0))
    monkeypatch.setattr(
        menu_repair.ingest,
        "run",
        lambda **kwargs: (
            ingest_calls.append(kwargs["restaurant_ids"])
            or {"succeeded": 1, "failed": 0}
        ),
    )

    summary = menu_repair.run(max_passes=3)

    assert ingest_calls == [[7], [7]]
    assert summary["repaired_restaurant_ids"] == [7]
    assert summary["remaining"] == 0


def test_repair_loop_stops_when_fingerprint_does_not_change(monkeypatch):
    audits = [[_finding("same")], [_finding("same")]]
    monkeypatch.setattr(menu_repair.menu_audit, "audit_menus", lambda: audits.pop(0))
    monkeypatch.setattr(
        menu_repair.ingest,
        "run",
        lambda **_kwargs: {"succeeded": 0, "failed": 1},
    )

    summary = menu_repair.run(max_passes=3)

    assert len(summary["passes"]) == 1
    assert summary["remaining"] == 1
