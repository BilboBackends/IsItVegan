"""Scrape Doctor plumbing — prompt, verdict parsing, endpoint guards.

The agent run itself (headless claude) is not tested here; these pin the
deterministic shell around it so the button can't mis-fire.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scrape_doctor  # noqa: E402


def test_prompt_carries_context_and_skill_pointer():
    prompt = scrape_doctor.build_prompt(
        {"id": 42, "name": "Cafe X", "website_url": "https://x.com",
         "address": "1 Main St, Orlando, FL"},
        {"crawl_method": "http", "menu_score": 0.4, "char_count": 900,
         "consecutive_failures": 3, "last_error": "No real menu found",
         "menu_urls": '["https://x.com/menu"]'},
    )
    assert ".claude/skills/scrape-doctor/SKILL.md" in prompt
    assert "id=42" in prompt and "Cafe X" in prompt
    assert "https://x.com" in prompt
    assert "No real menu found" in prompt
    assert "SCRAPE-DOCTOR RESULT" in prompt


def test_prompt_survives_missing_profile():
    prompt = scrape_doctor.build_prompt(
        {"id": 7, "name": "Y", "website_url": None, "address": None}, None
    )
    assert "id=7" in prompt


def test_result_line_parses_all_verdicts():
    for verdict in ("fixed", "unscrapeable", "failed"):
        match = scrape_doctor._RESULT_RE.search(
            f"...analysis...\nSCRAPE-DOCTOR RESULT: {verdict} — menu was behind a JS wall"
        )
        assert match and match.group(1).lower() == verdict
        assert "JS wall" in match.group(2)


def test_start_rejects_concurrent_runs():
    with scrape_doctor._state_lock:
        previous = dict(scrape_doctor._state)
        scrape_doctor._state["running"] = True
    try:
        try:
            scrape_doctor.start(1)
            assert False, "should have raised"
        except RuntimeError:
            pass
    finally:
        with scrape_doctor._state_lock:
            scrape_doctor._state.update(previous)


def test_skill_file_exists_with_result_contract():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        ".claude", "skills", "scrape-doctor", "SKILL.md",
    )
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert "SCRAPE-DOCTOR RESULT" in text
    # The two hard-won lessons must stay in the methodology.
    assert "_HTTP_HEADERS" in text        # reproduce with scraper's own UA
    assert "Fix the class" in text        # generic fixes only


def test_api_validation():
    from api import app

    client = app.test_client()
    assert client.post("/api/scrape-fix", json={}).status_code == 400
    assert (
        client.post("/api/scrape-fix", json={"restaurant_id": 99999999}).status_code
        == 404
    )
    assert client.get("/api/scrape-fix/status").status_code == 200
