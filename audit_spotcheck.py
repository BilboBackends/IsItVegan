"""Spot-check auditor: re-verify cheap-model verdicts with a frontier model.

The trust loop for handing classification to a cheap tier (DeepSeek):

1. Sample dishes whose LATEST classification came from the cheap model.
2. Re-classify just those dishes with a trusted reference (Claude
   subscription by default — same provider chain rules as everywhere else).
3. Record agreements/disagreements in classification_audits (monitoring),
   and store each disagreement as a correction in classifier_corrections —
   which learning.py injects into the cheap model's next prompt, so it
   learns from exactly the mistakes the audit caught.

Runnable in isolation (per CLAUDE.md conventions):

    python audit_spotcheck.py                  # 10 random deepseek dishes
    python audit_spotcheck.py --sample 25
    python audit_spotcheck.py --reference codex
    python audit_spotcheck.py --model-like "deepseek%"
"""
from __future__ import annotations

import argparse
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db
from classification_providers import run_provider
from classifier import VERDICTS, _SYSTEM

# vegan <-> likely_vegan is a calibration nuance, not a safety failure; both
# gates (strict counts) and users see them differently from vegan vs
# not_vegan. Adjacent pairs count as agreement.
_ADJACENT = {
    frozenset({"vegan", "likely_vegan"}),
    frozenset({"likely_vegan", "vegan_adaptable"}),
    frozenset({"unclear", "likely_vegan"}),
    frozenset({"unclear", "vegan_adaptable"}),
    frozenset({"unclear", "not_vegan"}),
}

_RECHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "confidence": {"type": "number"},
                    "reasoning": {"type": "string"},
                },
                "required": ["name", "verdict", "confidence", "reasoning"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


def _agreement(a: str, b: str) -> str:
    if a == b:
        return "agree"
    if frozenset({a, b}) in _ADJACENT:
        return "agree"  # soft agreement — calibration, not correctness
    return "disagree"


def run(
    sample: int = 10,
    reference: str = "claude",
    model_like: str = "deepseek%",
) -> dict:
    """Audit `sample` dishes; returns a summary dict. Never raises on model
    errors — a failed audit run records nothing rather than junk."""
    db.init_db()
    dishes = db.sample_recent_classifications(model_like, limit=sample)
    if not dishes:
        print(f"No dishes classified by models matching {model_like!r}.")
        return {"checked": 0, "agree": 0, "disagree": 0, "agreement": None}

    listing = "\n".join(
        f"{i + 1}. {d['name']}"
        + (f" — {d['raw_description']}" if d.get("raw_description") else "")
        + f" (restaurant: {d['restaurant_name']}"
        + (f", {d['primary_type']}" if d.get("primary_type") else "")
        + ")"
        for i, d in enumerate(dishes)
    )
    prompt = (
        "AUDIT MODE — classify ONLY the vegan verdict for each dish below "
        "(no extraction; the dishes are given). Use the exact dish names in "
        "your output. Judge each dish exactly as the system rules describe.\n\n"
        + listing
    )
    print(f"Spot-checking {len(dishes)} dish(es) against {reference}...")
    response = run_provider(
        requested=reference,
        system_prompt=_SYSTEM,
        user_prompt=prompt,
        schema=_RECHECK_SCHEMA,
    )
    if not response.ok or not response.data:
        print(f"Reference model failed: {response.error}")
        return {"checked": 0, "agree": 0, "disagree": 0,
                "agreement": None, "error": response.error}

    by_name = {
        (v.get("name") or "").strip().casefold(): v
        for v in response.data.get("verdicts", [])
        if isinstance(v, dict) and v.get("verdict") in VERDICTS
    }

    audits: list[dict] = []
    agree = disagree = 0
    for d in dishes:
        ref = by_name.get(d["name"].strip().casefold())
        if ref is None:
            continue  # reference skipped it; no signal either way
        status = _agreement(d["verdict"], ref["verdict"])
        audits.append({
            "restaurant_id": d["restaurant_id"],
            "dish_name": d["name"],
            "model": d["model_version"],
            "check_type": "spot_check",
            "rule": "verdict_match",
            "status": status,
            "detail": (
                f"cheap={d['verdict']} ({d['confidence']:.2f}) vs "
                f"{reference}={ref['verdict']} — {ref.get('reasoning', '')}"
            )[:400],
            "expected_verdict": ref["verdict"],
            "actual_verdict": d["verdict"],
        })
        if status == "agree":
            agree += 1
            continue
        disagree += 1
        # Feed the learning loop: the cheap model sees this correction in
        # its prompt on the next run.
        db.record_correction(
            d["name"],
            d["verdict"],
            ref["verdict"],
            description=d.get("raw_description"),
            note=(ref.get("reasoning") or "")[:200] or None,
        )
        print(
            f"  [disagree] {d['name']}: {d['verdict']} -> "
            f"{ref['verdict']} ({ref.get('reasoning', '')})"
        )

    db.record_audits(audits, provider="deepseek")
    checked = agree + disagree
    agreement = round(agree / checked, 3) if checked else None
    print(
        f"Done: {checked} compared, {agree} agree, {disagree} disagree"
        + (f" — {agreement:.0%} agreement" if agreement is not None else "")
    )
    if disagree:
        print(f"{disagree} correction(s) recorded — the cheap model will see "
              "them as learned examples on its next run.")
    return {"checked": checked, "agree": agree, "disagree": disagree,
            "agreement": agreement}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Spot-check cheap-model classifications against a "
        "frontier reference model."
    )
    parser.add_argument("--sample", type=int, default=10)
    parser.add_argument(
        "--reference", default="claude",
        help="Provider chain for the reference model (default: claude).",
    )
    parser.add_argument(
        "--model-like", default="deepseek%",
        help="SQL LIKE pattern for which model's output to audit.",
    )
    args = parser.parse_args()
    summary = run(
        sample=args.sample,
        reference=args.reference,
        model_like=args.model_like,
    )
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
