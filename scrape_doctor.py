"""Scrape Doctor: agentic deep-dive on one failed menu scrape.

Launches a headless Claude Code or Codex session (subscription-billed)
in THIS repo, pointed at .claude/skills/scrape-doctor/SKILL.md — the encoded
version of the manual debugging we've done by hand (Sixty Vines' hidden
daypart pages, Pepe's stale event PDF). The agent reproduces the failure
with the real scraper, finds the root cause on the live site, fixes the
scraper GENERICALLY, adds a regression test, verifies (pytest + live
re-scrape), and commits. Its final line is machine-parseable:

    SCRAPE-DOCTOR RESULT: <fixed|unscrapeable|failed> — <summary>

One job at a time; the Admin polls status() for a live log. Runnable in
isolation per repo convention:

    python scrape_doctor.py 124
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import db

_REPO_ROOT = str(Path(__file__).resolve().parent)

# The agent edits scraper code and runs pytest/git — it needs file edits and
# bash. WebFetch/WebSearch let it read docs; everything else stays default.
_ALLOWED_TOOLS = "Bash,Edit,Write,WebFetch,WebSearch"

_TIMEOUT_SECONDS = int(os.environ.get("SCRAPE_DOCTOR_TIMEOUT_SECONDS", "2400"))
_MODEL = os.environ.get("SCRAPE_DOCTOR_MODEL")  # None -> the CLI's default
_CODEX_MODEL = os.environ.get("SCRAPE_DOCTOR_CODEX_MODEL")
_AGENTS = frozenset({"claude", "codex"})

_RESULT_RE = re.compile(
    r"SCRAPE-DOCTOR RESULT:\s*(fixed|unscrapeable|failed)\s*[—-]?\s*(.*)",
    re.IGNORECASE,
)

_MAX_LOG_LINES = 300

_state_lock = threading.Lock()
_state: dict = {
    "running": False,
    "restaurant_id": None,
    "restaurant_name": None,
    "agent": None,
    "started_at": None,
    "log": [],           # rolling tail of agent activity, newest last
    "verdict": None,     # fixed | unscrapeable | failed | error
    "summary": None,
    "error": None,
}


def _log(line: str) -> None:
    line = (line or "").strip()
    if not line:
        return
    with _state_lock:
        _state["log"].append(line[:400])
        if len(_state["log"]) > _MAX_LOG_LINES:
            del _state["log"][: len(_state["log"]) - _MAX_LOG_LINES]


def build_prompt(
    restaurant: dict, profile: dict | None, agent: str = "claude"
) -> str:
    """The headless kickoff prompt. Context up front so the agent doesn't
    spend a round-trip re-querying what we already know; the skill carries
    the methodology."""
    lines = [
        "Read .claude/skills/scrape-doctor/SKILL.md and follow it exactly. "
        f"You are running through the {agent} CLI.",
        "",
        f"Target restaurant: id={restaurant['id']} — {restaurant['name']}",
        f"Website: {restaurant.get('website_url') or '(none on file)'}",
        f"Address: {restaurant.get('address') or '(unknown)'}",
    ]
    if profile:
        lines.append(
            "Crawl profile: method="
            + str(profile.get("crawl_method"))
            + f", score={profile.get('menu_score')}"
            + f", chars={profile.get('char_count')}"
            + f", consecutive_failures={profile.get('consecutive_failures')}"
        )
        if profile.get("last_error"):
            lines.append(f"Last error: {profile['last_error']}")
        if profile.get("menu_urls"):
            lines.append(f"Learned menu URLs: {profile['menu_urls']}")
    lines += [
        "",
        "Work autonomously; no one can answer questions mid-run. End with the",
        "SCRAPE-DOCTOR RESULT line the skill requires.",
    ]
    return "\n".join(lines)


def _agent_environment() -> dict:
    """Subscription auth remains external; scraper agents need no API keys."""
    environment = os.environ.copy()
    for name in (
        "ANTHROPIC_API_KEY",
        "CODEX_API_KEY",
        "DEEPSEEK_API_KEY",
        "GOOGLE_PLACES_API_KEY",
        "OPENAI_API_KEY",
    ):
        environment.pop(name, None)
    environment["NO_COLOR"] = "1"
    return environment


def _consume_claude_stream(process: subprocess.Popen) -> str | None:
    """Follow stream-json output, mirroring activity into the live log.

    Returns the final result text (the agent's last message) or None.
    """
    final_text: str | None = None
    for raw in process.stdout:  # type: ignore[union-attr]
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            _log(raw)
            continue
        kind = event.get("type")
        if kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    _log(block["text"])
                elif block.get("type") == "tool_use":
                    name = block.get("name", "tool")
                    hint = ""
                    tool_input = block.get("input") or {}
                    for key in ("command", "file_path", "pattern", "url"):
                        if tool_input.get(key):
                            hint = str(tool_input[key])[:120]
                            break
                    _log(f"[{name}] {hint}")
        elif kind == "result":
            final_text = event.get("result") or final_text
    return final_text


def _consume_codex_stream(process: subprocess.Popen) -> str | None:
    """Follow Codex ``exec --json`` events and return its last message."""
    final_text: str | None = None
    for raw in process.stdout:  # type: ignore[union-attr]
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            _log(raw)
            continue
        kind = event.get("type")
        item = event.get("item") or {}
        item_type = item.get("type")
        if kind == "item.completed" and item_type == "agent_message":
            text = item.get("text")
            if text:
                final_text = text
                _log(text)
        elif kind in {"item.started", "item.completed"}:
            if item_type == "command_execution" and item.get("command"):
                _log(f"[command] {str(item['command'])[:180]}")
            elif item_type in {"file_change", "mcp_tool_call", "web_search"}:
                hint = item.get("path") or item.get("name") or item.get("query") or ""
                _log(f"[{item_type}] {str(hint)[:180]}")
        elif kind in {"error", "turn.failed"}:
            detail = event.get("message") or (event.get("error") or {}).get("message")
            if detail:
                _log(f"[error] {detail}")
    return final_text


def _worktree_is_clean() -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode == 0 and not completed.stdout.strip()


def _codex_command(executable: str, output_path: Path) -> list[str]:
    """Bounded Codex invocation: repo writes + network, no sandbox bypass."""
    command = [
        executable,
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox", "workspace-write",
        "-c", "sandbox_workspace_write.network_access=true",
        "--color", "never",
        "--json",
        "--output-last-message", str(output_path),
        "-C", _REPO_ROOT,
        "-",
    ]
    if _CODEX_MODEL:
        command[2:2] = ["--model", _CODEX_MODEL]
    return command


def _run_job(restaurant: dict, profile: dict | None, agent: str) -> None:
    if agent == "claude":
        executable = shutil.which("claude")
    else:
        from classification_providers import find_codex

        executable = find_codex()
    if not executable:
        with _state_lock:
            _state.update(
                running=False, verdict="error",
                error=f"{agent.title()} CLI not found.",
            )
        return

    process = None
    try:
        prompt = build_prompt(restaurant, profile, agent=agent)
        with tempfile.TemporaryDirectory(prefix=f"scrape-doctor-{agent}-") as temp:
            output_path = Path(temp) / "last-message.txt"
            if agent == "claude":
                command = [
                    executable,
                    "-p",
                    prompt,
                    "--output-format", "stream-json",
                    "--verbose",
                    "--permission-mode", "acceptEdits",
                    "--allowedTools", _ALLOWED_TOOLS,
                ]
                if _MODEL:
                    command.extend(["--model", _MODEL])
            else:
                command = _codex_command(executable, output_path)

            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if agent == "codex" else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=_REPO_ROOT,
                env=_agent_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if agent == "codex" and process.stdin is not None:
                process.stdin.write(prompt)
                process.stdin.close()

            # Hard timebox: a hung browser or agent must not wedge Admin.
            timer = threading.Timer(_TIMEOUT_SECONDS, process.kill)
            timer.start()
            try:
                final_text = (
                    _consume_claude_stream(process)
                    if agent == "claude"
                    else _consume_codex_stream(process)
                )
                process.wait()
            finally:
                timer.cancel()
            if agent == "codex" and output_path.exists():
                final_text = output_path.read_text(
                    encoding="utf-8", errors="replace"
                ) or final_text

        verdict, summary = "failed", None
        if final_text:
            match = _RESULT_RE.search(final_text)
            if match:
                verdict = match.group(1).lower()
                summary = match.group(2).strip() or None
            else:
                summary = final_text.strip()[-400:]
        if process.returncode != 0 and verdict == "failed" and not summary:
            summary = f"{agent} exited with code {process.returncode}"
        with _state_lock:
            _state.update(running=False, verdict=verdict, summary=summary)
    except Exception as exc:  # never leave the job wedged in "running"
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
        with _state_lock:
            _state.update(
                running=False, verdict="error",
                error=f"{type(exc).__name__}: {exc}",
            )


def start(restaurant_id: int, agent: str = "claude") -> dict:
    """Kick off a fix job. Returns the initial state; raises on conflicts."""
    agent = (agent or "").strip().lower()
    if agent not in _AGENTS:
        raise ValueError("agent must be claude or codex.")
    with _state_lock:
        if _state["running"]:
            raise RuntimeError(
                f"A scrape-doctor run is already in progress "
                f"(restaurant {_state['restaurant_id']})."
            )
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id, name, website_url, address FROM restaurants "
                "WHERE id = ?",
                (restaurant_id,),
            ).fetchone()
        if row is None:
            raise LookupError(f"Restaurant {restaurant_id} not found.")
        if not _worktree_is_clean():
            raise RuntimeError(
                "Scrape Doctor requires a clean git worktree so it cannot "
                "commit unrelated changes. Commit or stash them first."
            )
        restaurant = dict(row)
        profile = db.get_crawl_profile(restaurant_id)
        _state.update(
            running=True,
            restaurant_id=restaurant_id,
            restaurant_name=restaurant["name"],
            agent=agent,
            started_at=time.time(),
            log=[],
            verdict=None,
            summary=None,
            error=None,
        )
    thread = threading.Thread(
        target=_run_job, args=(restaurant, profile, agent), daemon=True
    )
    thread.start()
    return status()


def status() -> dict:
    with _state_lock:
        return {**_state, "log": list(_state["log"])}


def main() -> None:
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Agentic scrape-failure fixer.")
    parser.add_argument("restaurant_id", type=int)
    parser.add_argument("--agent", choices=sorted(_AGENTS), default="claude")
    args = parser.parse_args()
    db.init_db()
    start(args.restaurant_id, agent=args.agent)
    while status()["running"]:
        time.sleep(2)
        state = status()
        for line in state["log"][-3:]:
            print("  ", line)
    state = status()
    print(f"\nverdict: {state['verdict']} — {state.get('summary')}")


if __name__ == "__main__":
    main()
