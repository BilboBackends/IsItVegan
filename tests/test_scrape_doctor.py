"""Scrape Doctor plumbing — prompt, verdict parsing, endpoint guards.

The agent run itself (headless claude) is not tested here; these pin the
deterministic shell around it so the button can't mis-fire.
"""
import os
import subprocess
import sys
from pathlib import Path

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


def test_codex_prompt_and_command_use_bounded_workspace_access():
    prompt = scrape_doctor.build_prompt(
        {"id": 8, "name": "Z", "website_url": "https://z.test", "address": None},
        None,
        agent="codex",
    )
    assert "codex CLI" in prompt
    assert ".git is intentionally read-only" in prompt
    assert "trusted launcher" in prompt
    command = scrape_doctor._codex_command("codex", Path("result.txt"))
    assert command[:2] == ["codex", "exec"]
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert 'windows.sandbox="unelevated"' in command
    assert "sandbox_workspace_write.network_access=true" in command
    assert "--json" in command and "--output-last-message" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command


def test_codex_commit_handoff_only_accepts_python_source_and_tests():
    accepted = (
        "scraper.py",
        "new_collector.py",
        "tests/test_scraper.py",
    )
    rejected = (
        ".env",
        "veganfind.db",
        "README.md",
        "frontend/public/data/restaurants.json",
        "tests/fixture.json",
        "../outside.py",
        "C:/outside.py",
    )
    assert all(scrape_doctor._codex_path_is_committable(path) for path in accepted)
    assert not any(scrape_doctor._codex_path_is_committable(path) for path in rejected)


def test_codex_commit_handoff_creates_commit_in_trusted_parent(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init")
    git("config", "user.email", "doctor@example.test")
    git("config", "user.name", "Scrape Doctor Test")
    source = repo / "scraper.py"
    source.write_text("before = True\n", encoding="utf-8")
    git("add", "scraper.py")
    git("commit", "-m", "baseline")
    source.write_text("after = True\n", encoding="utf-8")

    monkeypatch.setattr(scrape_doctor, "_REPO_ROOT", str(repo))
    monkeypatch.setattr(scrape_doctor, "_run_parent_test_suite", lambda: None)
    commit = scrape_doctor._commit_codex_changes(
        {"id": 42, "name": "Cafe X"}, "bounded retry verified"
    )

    assert commit == git("rev-parse", "--short", "HEAD")
    assert git("status", "--porcelain") == ""
    assert git("log", "-1", "--pretty=%s") == "Fix menu scraping exposed by Cafe X"


def test_result_line_parses_all_verdicts():
    for verdict in ("fixed", "recovered", "unscrapeable", "failed"):
        match = scrape_doctor._RESULT_RE.search(
            f"...analysis...\nSCRAPE-DOCTOR RESULT: {verdict} — menu was behind a JS wall"
        )
        assert match and match.group(1).lower() == verdict
        assert "JS wall" in match.group(2)


def test_recovered_requires_an_unchanged_worktree(monkeypatch):
    monkeypatch.setattr(scrape_doctor, "_worktree_is_clean", lambda: False)

    error = scrape_doctor._recovered_worktree_error("recovered")

    assert error and "uncommitted worktree changes" in error
    assert scrape_doctor._recovered_worktree_error("fixed") is None


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
    assert client.post(
        "/api/scrape-fix", json={"restaurant_id": True}
    ).status_code == 400
    assert client.post(
        "/api/scrape-fix", json={"restaurant_id": 1, "agent": "other"}
    ).status_code == 400
    assert (
        client.post("/api/scrape-fix", json={"restaurant_id": 99999999}).status_code
        == 404
    )
    assert client.get("/api/scrape-fix/status").status_code == 200


def test_api_rejects_pipeline_job_conflicts():
    import api

    client = api.app.test_client()
    previous_ingest = api._ingest_state["running"]
    previous_classify = api._classify_state["running"]
    try:
        api._ingest_state["running"] = True
        assert client.post(
            "/api/scrape-fix", json={"restaurant_id": 1}
        ).status_code == 409
        api._ingest_state["running"] = False
        api._classify_state["running"] = True
        assert client.post(
            "/api/scrape-fix", json={"restaurant_id": 1}
        ).status_code == 409
    finally:
        api._ingest_state["running"] = previous_ingest
        api._classify_state["running"] = previous_classify


def test_pipeline_endpoints_reject_an_active_deep_dive(monkeypatch):
    import api

    with scrape_doctor._state_lock:
        previous = dict(scrape_doctor._state)
        scrape_doctor._state["running"] = True
    monkeypatch.setattr(
        api.ingest,
        "run",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("ingest must not start during a deep dive")
        ),
    )
    monkeypatch.setattr(
        api,
        "_start_classify_job",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("classification must not start during a deep dive")
        ),
    )
    try:
        client = api.app.test_client()
        assert client.post(
            "/api/ingest", json={"restaurant_id": 1}
        ).status_code == 409
        assert client.post(
            "/api/classify", json={"restaurant_id": 1, "provider": "deepseek"}
        ).status_code == 409
    finally:
        with scrape_doctor._state_lock:
            scrape_doctor._state.update(previous)


def test_ingest_and_classification_jobs_are_mutually_exclusive(monkeypatch):
    import api

    client = api.app.test_client()
    previous_ingest = api._ingest_state["running"]
    previous_classify = api._classify_state["running"]
    monkeypatch.setattr(
        api.ingest,
        "run",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("ingest must not start during classification")
        ),
    )
    try:
        api._classify_state["running"] = True
        assert client.post(
            "/api/ingest", json={"restaurant_id": 1}
        ).status_code == 409

        api._classify_state["running"] = False
        api._ingest_state["running"] = True
        assert client.post(
            "/api/classify", json={"restaurant_id": 1, "provider": "deepseek"}
        ).status_code == 409
    finally:
        api._ingest_state["running"] = previous_ingest
        api._classify_state["running"] = previous_classify


def test_api_surfaces_missing_website_as_bad_request(monkeypatch):
    import api

    monkeypatch.setattr(
        scrape_doctor,
        "start",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("Restaurant 42 has no website to deep-dive.")
        ),
    )

    response = api.app.test_client().post(
        "/api/scrape-fix", json={"restaurant_id": 42}
    )

    assert response.status_code == 400
    assert "no website" in response.get_json()["error"]


def test_api_passes_success_pipeline_to_scrape_doctor(monkeypatch):
    import api

    captured = {}

    def fake_start(restaurant_id, agent, on_fixed):
        captured.update(
            restaurant_id=restaurant_id, agent=agent, on_fixed=on_fixed
        )
        return {"running": True}

    monkeypatch.setattr(scrape_doctor, "start", fake_start)
    response = api.app.test_client().post(
        "/api/scrape-fix", json={"restaurant_id": 42, "agent": "codex"}
    )

    assert response.status_code == 200
    assert captured == {
        "restaurant_id": 42,
        "agent": "codex",
        "on_fixed": api._finish_scrape_doctor_pipeline,
    }


def test_success_pipeline_ingests_then_starts_deepseek(monkeypatch):
    import api

    calls = []
    monkeypatch.setattr(
        api.ingest,
        "run",
        lambda **kwargs: calls.append(("ingest", kwargs))
        or {"succeeded": 1, "failed": 0, "failures": []},
    )
    monkeypatch.setattr(
        api,
        "_start_classify_job",
        lambda **kwargs: calls.append(("classify", kwargs)) or (None, "deepseek"),
    )

    result = api._finish_scrape_doctor_pipeline(42)

    assert calls == [
        ("ingest", {"restaurant_id": 42}),
        (
            "classify",
            {
                "requested_provider": "deepseek",
                "restaurant_id": 42,
                "parallel": 1,
                "mode": "auto",
            },
        ),
    ]
    assert "DeepSeek classification started" in result


def test_success_pipeline_does_not_classify_a_failed_rescrape(monkeypatch):
    import api

    monkeypatch.setattr(
        api.ingest,
        "run",
        lambda **_kwargs: {
            "succeeded": 0,
            "failed": 1,
            "failures": [("Cafe X", "still blocked")],
        },
    )
    monkeypatch.setattr(
        api,
        "_start_classify_job",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("classification must not start")
        ),
    )

    try:
        api._finish_scrape_doctor_pipeline(42)
        assert False, "expected a failed re-scrape"
    except RuntimeError as exc:
        assert "re-scrape failed" in str(exc)
