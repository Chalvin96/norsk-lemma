"""Shared LLM transport: config, errors, fenced-JSON parsing, quota detection.

One deep module for the plumbing behind every gloss/translate call. Callers own
prompt building and response-shape validation; this module owns transport
(HTTP + CLI subprocess), retries, and fence stripping. Adapters live in the
sibling steps of this task and Task 2; this step is the shared vocabulary.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import requests

from .settings import (
    CLI_TIMEOUT,
    DEFAULT_RETRIES,
    DEFAULT_RETRY_DELAY,
    OPENROUTER_URL,
    REQUEST_TIMEOUT,
    logger,
)


class InputMode(Enum):
    STDIN = "stdin"
    ARG = "arg"
    FILE = "file"


class OutputMode(Enum):
    STDOUT = "stdout"
    FILE = "file"


# Substrings (case-insensitive) that mean an account billing / usage / rate
# limit was hit and retrying will NOT help — stop instead of burning retries.
_QUOTA_ERROR_MARKERS = (
    "usage limit",
    "quota",
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
    "insufficient",
    "billing",
    "payment required",
    "402",
    "credit",
    "limit reached",
    "limit exceeded",
    "exceeded your",
)

# Short cap for transient network blips; rate-limit sleeps use the full retry_delay.
_NETWORK_ERROR_MAX_SLEEP = 5
_TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})

# Conservative process-argument headroom for arg-input harnesses (opencode/pi).
# Windows counts UTF-16 code units and has a much smaller CreateProcess limit.
_MAX_ARG_PROMPT_BYTES = 100_000
_WINDOWS_MAX_ARG_PROMPT_CODE_UNITS = 28_000


def is_quota_error(message: str) -> bool:
    """Return True if *message* signals a billing/usage/rate limit (hard stop)."""
    low = (message or "").lower()
    return any(marker in low for marker in _QUOTA_ERROR_MARKERS)


@dataclass(frozen=True)
class LlmConfig:
    model: str
    harness: str = "openrouter"
    max_retries: int = DEFAULT_RETRIES
    retry_delay: int = DEFAULT_RETRY_DELAY
    reasoning_effort: str | None = None
    response_schema: dict[str, Any] | None = None


class LlmError(Exception):
    """Transport-level failure. ``code`` is the stable machine-readable token."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def parse_fenced_json(content: str) -> Any | None:
    """Strip a markdown code fence and parse JSON; regex fallback; None on failure."""
    content = content.strip()
    if content.startswith("```"):
        first_newline = content.find("\n")
        if first_newline != -1:
            content = content[first_newline + 1 :]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match is None:
            return None
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return None


def complete_openrouter(
    session: requests.Session,
    config: LlmConfig,
    prompt: str,
    *,
    max_tokens: int,
    temperature: float = 0.1,
) -> str:
    """POST to OpenRouter with retry on transient errors; return message content."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise LlmError("missing_api_key")

    payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if config.response_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "translations",
                "strict": True,
                "schema": config.response_schema,
            },
        }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    for attempt in range(1, config.max_retries + 1):
        try:
            response = session.post(
                OPENROUTER_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            if attempt == config.max_retries:
                raise LlmError(f"request_error: {exc}") from exc
            logger.warning("Request failed on attempt %s/%s: %s", attempt, config.max_retries, exc)
            time.sleep(min(_NETWORK_ERROR_MAX_SLEEP, config.retry_delay))
            continue

        if response.status_code in _TRANSIENT_HTTP and attempt < config.max_retries:
            logger.warning(
                "Transient HTTP %s on attempt %s/%s",
                response.status_code,
                attempt,
                config.max_retries,
            )
            time.sleep(config.retry_delay)
            continue
        if response.status_code != 200:
            raise LlmError(f"http_{response.status_code}", detail=response.text[:200])

        try:
            return response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmError(f"invalid_response: {exc}") from exc

    raise LlmError("unknown_error")


# NOTE: harnesses differ only in flags + input/output mode today, so this
# stays a data-driven strategy. If a harness needs real per-tool BEHAVIOR
# (custom output extraction, auth preflight, per-tool quota markers,
# streaming), refactor to an ABC with a template run() + overridable
# build_argv/read_output/quota_markers instead of adding name checks here.
@dataclass(frozen=True)
class CliHarness:
    name: str
    build_argv: Callable[..., list[str]]
    input_mode: InputMode
    output_mode: OutputMode


def _codex_argv(config: LlmConfig, output_path, prompt_path) -> list[str]:
    effort = (
        ["-c", f"model_reasoning_effort={config.reasoning_effort}"]
        if config.reasoning_effort
        else []
    )
    return [
        "codex",
        "exec",
        "--model",
        config.model,
        *effort,
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--skip-git-repo-check",
        "--output-last-message",
        str(output_path),
        "-",
    ]


def _claude_argv(config: LlmConfig, output_path, prompt_path) -> list[str]:
    # Tools disabled so a stray tool call can't derail a pure-completion gloss.
    return [
        "claude",
        "-p",
        "--model",
        config.model,
        "--output-format",
        "text",
        "--disallowedTools",
        "*",
    ]


def _opencode_argv(config: LlmConfig, output_path, prompt_path) -> list[str]:
    variant = ["--variant", config.reasoning_effort] if config.reasoning_effort else []
    # --pure: skip external plugins so repo/user config can't alter the output.
    return ["opencode", "run", "--pure", "--model", config.model, "--format", "default", *variant]


def _droid_argv(config: LlmConfig, output_path, prompt_path) -> list[str]:
    effort = ["-r", config.reasoning_effort] if config.reasoning_effort else []
    return ["droid", "exec", "-o", "text", "-m", config.model, *effort, "-f", str(prompt_path)]


def _pi_argv(config: LlmConfig, output_path, prompt_path) -> list[str]:
    thinking = ["--thinking", config.reasoning_effort] if config.reasoning_effort else []
    return [
        "pi",
        "-p",
        "--mode",
        "text",
        "--no-tools",
        "--no-session",
        "--no-context-files",
        "--model",
        config.model,
        *thinking,
    ]


HARNESSES: dict[str, CliHarness] = {
    "codex": CliHarness(
        "codex", _codex_argv, input_mode=InputMode.STDIN, output_mode=OutputMode.FILE
    ),
    "claude": CliHarness(
        "claude", _claude_argv, input_mode=InputMode.STDIN, output_mode=OutputMode.STDOUT
    ),
    "opencode": CliHarness(
        "opencode", _opencode_argv, input_mode=InputMode.ARG, output_mode=OutputMode.STDOUT
    ),
    "droid": CliHarness(
        "droid", _droid_argv, input_mode=InputMode.FILE, output_mode=OutputMode.STDOUT
    ),
    "pi": CliHarness("pi", _pi_argv, input_mode=InputMode.ARG, output_mode=OutputMode.STDOUT),
}


def complete_cli(harness: CliHarness, config: LlmConfig, prompt: str) -> str:
    """Run *prompt* through an agentic CLI harness; return its last message.

    Runs in a throwaway temp directory (so no repo CLAUDE.md/AGENTS.md leaks in),
    non-interactively, with tools disabled / read-only per harness. Quota errors
    raise at once; other non-zero exits and empty output retry up to max_retries.
    """
    if harness.input_mode == InputMode.ARG:
        prompt_size = len(prompt.encode("utf-8"))
        limit = _MAX_ARG_PROMPT_BYTES
        unit = "bytes"
        if os.name == "nt":
            prompt_size = len(prompt.encode("utf-16-le")) // 2
            limit = _WINDOWS_MAX_ARG_PROMPT_CODE_UNITS
            unit = "UTF-16 code units"
        if prompt_size > limit:
            raise LlmError(
                f"{harness.name}_prompt_too_large",
                f"{prompt_size} {unit} exceeds arg limit; lower --batch-size",
            )

    for attempt in range(1, config.max_retries + 1):
        content: str | None = None
        diagnostic = ""
        with tempfile.TemporaryDirectory() as workdir:
            work = Path(workdir)
            output_path = work / "out.txt" if harness.output_mode == OutputMode.FILE else None
            prompt_path = None
            if harness.input_mode == InputMode.FILE:
                prompt_path = work / "prompt.txt"
                prompt_path.write_text(prompt, encoding="utf-8")

            argv = harness.build_argv(config, output_path, prompt_path)
            if harness.input_mode == InputMode.ARG:
                argv = [*argv, prompt]
            stdin_text = prompt if harness.input_mode == InputMode.STDIN else None

            # Never leave stdin inherited from the parent for arg/file harnesses —
            # a CLI that falls back to reading a TTY would hang until CLI_TIMEOUT.
            run_kwargs: dict[str, Any] = dict(
                text=True,
                encoding="utf-8",
                capture_output=True,
                cwd=workdir,
                timeout=CLI_TIMEOUT,
                check=False,
            )
            if stdin_text is not None:
                run_kwargs["input"] = stdin_text
            else:
                run_kwargs["stdin"] = subprocess.DEVNULL

            try:
                completed = subprocess.run(argv, **run_kwargs)
            except FileNotFoundError as exc:
                # Binary not installed — retrying cannot fix it.
                raise LlmError(f"{harness.name}_error", str(exc)) from exc
            except (OSError, subprocess.SubprocessError) as exc:
                if attempt == config.max_retries:
                    raise LlmError(f"{harness.name}_error", str(exc)) from exc
                time.sleep(min(_NETWORK_ERROR_MAX_SLEEP, config.retry_delay))
                continue

            if completed.returncode != 0:
                message = (completed.stderr or completed.stdout or "").strip()
                if is_quota_error(message):
                    raise LlmError(f"{harness.name}_quota", message[:500])
                if attempt == config.max_retries:
                    raise LlmError(f"{harness.name}_exit_{completed.returncode}", message[:500])
                logger.warning(
                    "%s exited %s on attempt %s/%s: %s",
                    harness.name,
                    completed.returncode,
                    attempt,
                    config.max_retries,
                    message[:200],
                )
                time.sleep(config.retry_delay)
                continue

            if harness.output_mode == OutputMode.FILE:
                content = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
            else:
                content = completed.stdout
            diagnostic = (completed.stderr or "").strip()

        if content and content.strip():
            return content
        # Exit 0 but no usable answer: a quota/limit message on stderr means stop,
        # not retry (the CLI swallowed the limit into a clean exit).
        if is_quota_error(diagnostic):
            raise LlmError(f"{harness.name}_quota", diagnostic[:500])
        if attempt == config.max_retries:
            raise LlmError(f"{harness.name}_empty_response")
        time.sleep(min(_NETWORK_ERROR_MAX_SLEEP, config.retry_delay))

    raise LlmError("unknown_error")


def _cli_provider(harness: CliHarness) -> Callable[..., str]:
    def run(
        _session,
        config: LlmConfig,
        prompt: str,
        *,
        max_tokens: int = 0,
        temperature: float = 0.1,
    ) -> str:
        # max_tokens/temperature are HTTP-API knobs; CLI harnesses ignore them.
        return complete_cli(harness, config, prompt)

    return run


PROVIDERS: dict[str, Callable[..., str]] = {
    "openrouter": complete_openrouter,
    **{name: _cli_provider(harness) for name, harness in HARNESSES.items()},
}
