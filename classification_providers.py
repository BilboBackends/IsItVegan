"""Model transports for menu classification.

The extraction prompt, JSON schema, validation, and persistence remain shared
in classifier.py/classify.py. This module only obtains schema-shaped JSON from
an available model provider.

Three transports:
- claude    — headless Claude Code CLI (`claude -p`), billed to the user's
              Claude subscription
- codex     — Codex CLI (`codex exec`), billed to the user's ChatGPT
              subscription
- anthropic — the Anthropic API, billed per token to ANTHROPIC_API_KEY

The provider setting is a priority CHAIN, not a single choice: "auto" means
claude then codex — subscriptions only; the metered API runs ONLY when
explicitly selected — and a custom order like "codex,claude,anthropic" works
too. When a provider fails (most importantly: hits its subscription usage
limit), the next one in the chain is tried, and a limit-hit provider is
skipped for a cooldown window so a 50-restaurant run doesn't knock on a
closed door 50 times.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from config import settings

# $/MTok (input, output) for API cost reporting. Approximate list prices.
# Single source of truth — classifier.py imports this for its estimates.
PRICES = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

_PROVIDER_NAMES = ("claude", "codex", "anthropic")
# "auto" is SUBSCRIPTIONS ONLY. The metered Anthropic API never runs unless
# the user explicitly selects it (alone or in a custom chain) — hitting both
# subscription limits should stop the run, not silently start billing.
_AUTO_CHAIN = ("claude", "codex")

_BILLING = {
    "claude": "claude_subscription",
    "codex": "chatgpt_subscription",
    "anthropic": "api",
}

# Subscription usage-limit / rate-limit signatures across providers. A match
# puts the provider in cooldown so the chain stops retrying it every dish.
_LIMIT_RE = re.compile(
    r"rate.?limit|usage limit|limit (?:reached|exceeded)|quota|"
    r"too many requests|\b429\b|insufficient.{0,8}credit|out of credits|"
    r"overloaded",
    re.IGNORECASE,
)
_LIMIT_COOLDOWN_SECONDS = 20 * 60
_limited_until: dict[str, float] = {}

# Availability probes are cached with a TTL (not forever) so logging in to a
# CLI is noticed without restarting the backend.
_AVAILABILITY_TTL_SECONDS = 300
_availability_cache: dict[str, tuple[float, bool]] = {}


@dataclass
class ProviderResponse:
    ok: bool
    provider: str
    model: str
    billing: str
    data: dict | None = None
    error: str | None = None
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_estimate: float = 0.0


class ProviderUnavailable(RuntimeError):
    pass


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _cached_probe(name: str, probe) -> bool:
    now = time.monotonic()
    hit = _availability_cache.get(name)
    if hit is not None and now - hit[0] < _AVAILABILITY_TTL_SECONDS:
        return hit[1]
    value = bool(probe())
    _availability_cache[name] = (now, value)
    return value


def find_codex() -> str | None:
    """Locate the codex CLI: PATH, then CODEX_CLI_PATH, then the copy bundled
    inside the ChatGPT VS Code extension (a full CLI sharing ~/.codex auth —
    how Codex is installed on machines that only use the IDE extension)."""
    path = shutil.which("codex")
    if path:
        return path
    override = settings.codex_cli_path
    if override and Path(override).exists():
        return override
    extensions = Path.home() / ".vscode" / "extensions"
    candidates = [
        p
        for p in extensions.glob("openai.chatgpt-*/bin/*/codex*")
        if p.stem == "codex" and p.is_file()
    ]
    if candidates:
        # Newest extension version wins (best-effort string sort).
        return str(sorted(candidates, key=lambda p: str(p), reverse=True)[0])
    return None


def _probe_codex() -> bool:
    executable = find_codex()
    if not executable:
        return False
    try:
        result = subprocess.run(
            [executable, "login", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_no_window(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "logged in" in (
        (result.stdout or "") + (result.stderr or "")
    ).lower()


def codex_available() -> bool:
    return _cached_probe("codex", _probe_codex)


def claude_available() -> bool:
    # Installed is enough: `claude -p` uses the CLI's own OAuth login, and an
    # auth failure at run time surfaces as an error the chain fails over on.
    return _cached_probe("claude", lambda: shutil.which("claude") is not None)


def _provider_available(name: str) -> bool:
    if name == "claude":
        return claude_available()
    if name == "codex":
        return codex_available()
    return bool(settings.anthropic_api_key)


def provider_limited(name: str) -> bool:
    return _limited_until.get(name, 0.0) > time.monotonic()


def _mark_limited(name: str) -> None:
    _limited_until[name] = time.monotonic() + _LIMIT_COOLDOWN_SECONDS


def _is_limit_error(text: str | None) -> bool:
    return bool(text) and bool(_LIMIT_RE.search(text))


def _provider_chain(requested: str | None) -> list[str]:
    """Parse a provider request into a priority chain.

    "auto" -> claude, codex, anthropic (subscriptions before metered API).
    A single name pins that provider; a comma list is a custom priority order.
    """
    raw = (requested or settings.classifier_provider or "auto").strip().lower()
    names = list(_AUTO_CHAIN) if raw == "auto" else [
        part.strip() for part in raw.split(",") if part.strip()
    ]
    for name in names:
        if name not in _PROVIDER_NAMES:
            raise ProviderUnavailable(
                "Classifier provider must be auto, claude, codex, anthropic, "
                "or a comma-separated priority list of those."
            )
    if not names:
        raise ProviderUnavailable("Classifier provider must not be empty.")
    return names


def provider_status() -> dict:
    configured = settings.classifier_provider
    resolved = None
    try:
        resolved = resolve_provider(configured)
    except ProviderUnavailable:
        pass
    return {
        "configured": configured,
        "resolved": resolved,
        "providers": {
            "claude": {
                "available": claude_available(),
                "limited": provider_limited("claude"),
                "billing": _BILLING["claude"],
                "model": settings.claude_classifier_model
                or "Claude Code default",
            },
            "codex": {
                "available": codex_available(),
                "limited": provider_limited("codex"),
                "billing": _BILLING["codex"],
                "model": settings.codex_classifier_model or "Codex default",
            },
            "anthropic": {
                "available": bool(settings.anthropic_api_key),
                "limited": provider_limited("anthropic"),
                "billing": _BILLING["anthropic"],
                "model": settings.anthropic_classifier_model,
            },
        },
    }


def resolve_provider(requested: str | None = None) -> str:
    """First available provider in the requested chain (cooldowns ignored —
    this answers "can a run start?", not "who serves the next request?")."""
    names = _provider_chain(requested)
    for name in names:
        if _provider_available(name):
            return name
    raise ProviderUnavailable(
        "No classifier provider is available (tried: "
        + ", ".join(names)
        + "). Log in to Claude Code or Codex — or explicitly select "
        "anthropic to bill the API."
    )


def run_provider(
    *,
    requested: str | None,
    system_prompt: str,
    user_prompt: str,
    schema: dict,
) -> ProviderResponse:
    """Get schema-shaped JSON from the first provider in the chain that can.

    Walks the chain in priority order; any failure falls through to the next
    provider, and a usage-limit failure puts that provider in cooldown so the
    rest of a bulk run skips it. If every non-limited provider fails, limited
    ones are retried once rather than failing the run outright.
    """
    names = _provider_chain(requested)
    transports = {
        "claude": _run_claude,
        "codex": _run_codex,
        "anthropic": _run_anthropic,
    }
    last: ProviderResponse | None = None
    attempted: set[str] = set()

    for skip_limited in (True, False):
        for name in names:
            if name in attempted or not _provider_available(name):
                continue
            if skip_limited and provider_limited(name):
                continue
            attempted.add(name)
            response = transports[name](system_prompt, user_prompt, schema)
            if response.ok:
                return response
            if _is_limit_error(response.error):
                _mark_limited(name)
            last = response

    if last is not None:
        return last
    raise ProviderUnavailable(
        "No classifier provider is available (tried: "
        + ", ".join(names)
        + "). Log in to Claude Code or Codex — or explicitly select "
        "anthropic to bill the API."
    )


def _run_anthropic(
    system_prompt: str, user_prompt: str, schema: dict
) -> ProviderResponse:
    model = settings.anthropic_classifier_model
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        kwargs = dict(
            model=model,
            max_tokens=64000,
            system=system_prompt,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user_prompt}],
        )
        if "haiku" not in model:
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"]["effort"] = "medium"
        with client.messages.stream(**kwargs) as stream:
            response = stream.get_final_message()
    except Exception as exc:
        return ProviderResponse(
            ok=False,
            provider="anthropic",
            model=model,
            billing="api",
            error=f"{type(exc).__name__}: {exc}",
        )

    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    input_price, output_price = PRICES.get(model, (5.0, 25.0))
    cost = (
        input_tokens * input_price + output_tokens * output_price
    ) / 1_000_000
    if response.stop_reason in {"max_tokens", "refusal"}:
        return ProviderResponse(
            ok=False,
            provider="anthropic",
            model=model,
            billing="api",
            error=(
                "Output hit max_tokens (menu too large?)"
                if response.stop_reason == "max_tokens"
                else "Model refused"
            ),
            stop_reason=response.stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate=cost,
        )
    text = next((block.text for block in response.content if block.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return ProviderResponse(
            ok=False,
            provider="anthropic",
            model=model,
            billing="api",
            error=f"Malformed JSON: {exc}",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_estimate=cost,
        )
    return ProviderResponse(
        ok=True,
        provider="anthropic",
        model=model,
        billing="api",
        data=data,
        stop_reason=response.stop_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate=cost,
    )


# Secrets the classification subprocesses must never see. Removing
# ANTHROPIC_API_KEY also matters for billing: with it present, the Claude CLI
# would bill the API key instead of the logged-in subscription.
_SCRUBBED_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "GOOGLE_PLACES_API_KEY",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
)


def _scrubbed_environment() -> dict:
    environment = os.environ.copy()
    for name in _SCRUBBED_ENV_VARS:
        environment.pop(name, None)
    environment["NO_COLOR"] = "1"
    return environment


def _run_claude(
    system_prompt: str, user_prompt: str, schema: dict
) -> ProviderResponse:
    """Classify via headless Claude Code (`claude -p`) on the user's
    Claude subscription.

    --json-schema constrains the output shape, --tools "" disables every
    built-in tool (this is a pure text-in/JSON-out call, not an agent), and
    the working directory is an empty temp dir so no project CLAUDE.md or
    memory is pulled into (and billed against) the request.
    """
    executable = shutil.which("claude")
    model = settings.claude_classifier_model or "claude-code-default"
    if not executable:
        return ProviderResponse(
            ok=False,
            provider="claude",
            model=model,
            billing=_BILLING["claude"],
            error="Claude Code CLI was not found on PATH.",
        )

    prompt = (
        "Act only as a structured menu-classification engine. Do not use "
        "tools, read files, or browse. Return every menu item as JSON "
        "matching the enforced output schema.\n\n"
        + system_prompt
        + "\n\n"
        + user_prompt
    )
    try:
        with tempfile.TemporaryDirectory(prefix="veganfind-claude-") as temp:
            command = [
                executable,
                "-p",
                "--output-format",
                "json",
                "--tools",
                "",
                "--json-schema",
                json.dumps(schema),
            ]
            if settings.claude_classifier_model:
                command.extend(["--model", settings.claude_classifier_model])
            completed = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=temp,
                env=_scrubbed_environment(),
                timeout=max(60, settings.claude_classifier_timeout_seconds),
                creationflags=_no_window(),
            )
    except subprocess.TimeoutExpired:
        return ProviderResponse(
            ok=False,
            provider="claude",
            model=model,
            billing=_BILLING["claude"],
            error=(
                "Claude Code classification timed out after "
                f"{settings.claude_classifier_timeout_seconds} seconds."
            ),
        )
    except OSError as exc:
        return ProviderResponse(
            ok=False,
            provider="claude",
            model=model,
            billing=_BILLING["claude"],
            error=f"{type(exc).__name__}: {exc}",
        )

    def _fail(detail: str) -> ProviderResponse:
        return ProviderResponse(
            ok=False,
            provider="claude",
            model=model,
            billing=_BILLING["claude"],
            error=detail[-1000:],
        )

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        return _fail(f"Claude Code exited {completed.returncode}: {detail}")
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return _fail(f"Claude Code emitted non-JSON output: {exc}")

    # modelUsage maps model id -> usage and includes internal helper calls
    # (e.g. a Haiku title/summarizer); the MAIN model is the one that did the
    # heavy output, so pick by output tokens rather than dict order.
    used_model = envelope.get("modelUsage") or {}
    if isinstance(used_model, dict) and used_model:
        def _out_tokens(entry) -> int:
            if not isinstance(entry, dict):
                return 0
            return int(
                entry.get("outputTokens") or entry.get("output_tokens") or 0
            )

        model = max(used_model, key=lambda k: _out_tokens(used_model[k]))
    usage = envelope.get("usage") or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    if envelope.get("is_error"):
        return _fail(str(envelope.get("result") or "Claude Code reported an error."))

    # With --json-schema the structured object rides in structured_output
    # (newer CLIs) or as the result text itself; accept either.
    data = envelope.get("structured_output")
    if data is None:
        raw = envelope.get("result")
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            text = raw.strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.S)
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                return _fail(f"Malformed JSON from Claude Code: {exc}")
        else:
            return _fail("Claude Code returned no result payload.")

    return ProviderResponse(
        ok=True,
        provider="claude",
        model=model,
        billing=_BILLING["claude"],
        data=data,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_estimate=0.0,  # subscription usage, not metered dollars
    )


def _run_codex(
    system_prompt: str, user_prompt: str, schema: dict
) -> ProviderResponse:
    executable = find_codex()
    model = settings.codex_classifier_model or "codex-default"
    if not executable:
        return ProviderResponse(
            ok=False,
            provider="codex",
            model=model,
            billing="chatgpt_subscription",
            error="Codex CLI was not found on PATH.",
        )

    prompt = (
        "Act only as a structured menu-classification engine. Do not inspect "
        "files, run commands, browse, or use tools. Return every menu item and "
        "obey the supplied JSON schema.\n\n"
        + system_prompt
        + "\n\n"
        + user_prompt
    )
    # Codex auth is stored outside the child environment; scrub unrelated API
    # secrets so the classification agent cannot accidentally expose them.
    environment = _scrubbed_environment()

    try:
        with tempfile.TemporaryDirectory(prefix="veganfind-codex-") as temp:
            temp_path = Path(temp)
            schema_path = temp_path / "classification-schema.json"
            output_path = temp_path / "classification-result.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            command = [
                executable,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ]
            if settings.codex_classifier_model:
                command.extend(["--model", settings.codex_classifier_model])
            command.append("-")
            completed = subprocess.run(
                command,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=temp,
                env=environment,
                timeout=max(60, settings.codex_classifier_timeout_seconds),
                creationflags=_no_window(),
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                return ProviderResponse(
                    ok=False,
                    provider="codex",
                    model=model,
                    billing="chatgpt_subscription",
                    error=f"Codex exited {completed.returncode}: {detail[-1000:]}",
                )
            data = json.loads(output_path.read_text(encoding="utf-8"))
    except subprocess.TimeoutExpired:
        return ProviderResponse(
            ok=False,
            provider="codex",
            model=model,
            billing="chatgpt_subscription",
            error=(
                "Codex classification timed out after "
                f"{settings.codex_classifier_timeout_seconds} seconds."
            ),
        )
    except (OSError, json.JSONDecodeError) as exc:
        return ProviderResponse(
            ok=False,
            provider="codex",
            model=model,
            billing="chatgpt_subscription",
            error=f"{type(exc).__name__}: {exc}",
        )
    return ProviderResponse(
        ok=True,
        provider="codex",
        model=model,
        billing="chatgpt_subscription",
        data=data,
        cost_estimate=0.0,
    )
