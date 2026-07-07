"""Feedback loop for the cheap classification tier.

"Learning" here is deliberately simple and inspectable — no fine-tuning:
spot-check disagreements (audit_spotcheck.py) are stored as corrections in
classifier_corrections, and the most recent active ones are injected into
the cheap model's system prompt as worked examples. The model literally sees
its own recent mistakes, with the right answer and why, every time it runs.

Corrections are data, so the loop is auditable: list them, deactivate bad
ones (UPDATE classifier_corrections SET active=0), and the prompt changes
accordingly on the next run. Frontier providers do NOT get this block — their
prompt stays the calibrated baseline that the corrections are measured
against.
"""
from __future__ import annotations

import db

# Prompt budget: each correction is one line (~25 tokens). 15 lines keeps the
# whole block under ~400 tokens — noise-level cost on a menu-sized prompt.
MAX_CORRECTIONS_IN_PROMPT = 15


def guidance_block(db_path: str | None = None) -> str | None:
    """The learned-corrections prompt block, or None when there are none."""
    corrections = db.list_corrections(
        active_only=True, limit=MAX_CORRECTIONS_IN_PROMPT, db_path=db_path
    )
    if not corrections:
        return None
    lines = [
        "LEARNED CORRECTIONS — an audit of your recent classifications found "
        "these mistakes. Apply the same reasoning to similar dishes:"
    ]
    for c in corrections:
        line = (
            f"- \"{c['dish_name']}\" was wrongly {c['wrong_verdict']}; "
            f"correct verdict: {c['correct_verdict']}"
        )
        if c.get("note"):
            line += f" ({c['note']})"
        lines.append(line)
    return "\n".join(lines)
