"""Tests for the shared LLM transport (ordbokene.llm)."""
import json
from types import SimpleNamespace

import pytest
import requests
from ordbokene import llm
from ordbokene.llm import (
    HARNESSES,
    PROVIDERS,
    InputMode,
    LlmConfig,
    LlmError,
    OutputMode,
    complete_cli,
    complete_openrouter,
    is_quota_error,
    parse_fenced_json,
)


def test_llm_config_defaults() -> None:
    config = LlmConfig(model="m")
    assert config.harness == "openrouter"
    assert config.reasoning_effort is None


def test_llm_error_str_with_and_without_detail() -> None:
    assert str(LlmError("http_404")) == "http_404"
    assert str(LlmError("http_404", "nope")) == "http_404: nope"
    assert LlmError("codex_quota", "limit").code == "codex_quota"


def test_parse_fenced_json_accepts_clean_fenced_and_embedded() -> None:
    expected = {"10": {"definitions": []}}
    clean = json.dumps(expected)
    assert parse_fenced_json(clean) == expected
    assert parse_fenced_json(f"```json\n{clean}\n```") == expected
    assert parse_fenced_json(f"Here you go:\n{clean}\nDone.") == expected


def test_parse_fenced_json_returns_none_on_garbage() -> None:
    assert parse_fenced_json("not json at all") is None
    assert parse_fenced_json("```\nstill { not json\n```") is None


@pytest.mark.parametrize(
    "message",
    ["HTTP 429 too many requests", "insufficient credit", "You exceeded your usage limit"],
)
def test_is_quota_error_matches_markers(message: str) -> None:
    assert is_quota_error(message)


def test_is_quota_error_ignores_unrelated() -> None:
    assert not is_quota_error("connection reset by peer")


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok(content="hello"):
    return _FakeResponse(200, {"choices": [{"message": {"content": content}}]})


_CFG = LlmConfig(model="test-model", max_retries=3, retry_delay=0)


def test_openrouter_missing_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(LlmError) as excinfo:
        complete_openrouter(_FakeSession([]), _CFG, "p", max_tokens=10)
    assert excinfo.value.code == "missing_api_key"


def test_openrouter_retries_transient_http_then_succeeds(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    session = _FakeSession([_FakeResponse(429), _ok("done")])
    assert complete_openrouter(session, _CFG, "p", max_tokens=10) == "done"
    assert session.calls == 2


def test_openrouter_network_error_exhausts_retries(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    session = _FakeSession([requests.ConnectionError("boom")] * 3)
    with pytest.raises(LlmError) as excinfo:
        complete_openrouter(session, _CFG, "p", max_tokens=10)
    assert excinfo.value.code.startswith("request_error:")
    assert session.calls == 3


def test_openrouter_non_transient_http_fails_fast(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    session = _FakeSession([_FakeResponse(404, text="nope")])
    with pytest.raises(LlmError) as excinfo:
        complete_openrouter(session, _CFG, "p", max_tokens=10)
    assert excinfo.value.code == "http_404"
    assert session.calls == 1


def test_openrouter_invalid_response_shape(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    session = _FakeSession([_FakeResponse(200, {"choices": []})])
    with pytest.raises(LlmError) as excinfo:
        complete_openrouter(session, _CFG, "p", max_tokens=10)
    assert excinfo.value.code.startswith("invalid_response:")


def test_openrouter_posts_expected_request(monkeypatch) -> None:
    """Golden regression: the default path's request body must not drift."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    captured = {}

    class _CapturingSession:
        def post(self, url, *, headers, json, timeout):
            captured.update(url=url, headers=headers, json=json, timeout=timeout)
            return _ok("out")

    assert complete_openrouter(_CapturingSession(), _CFG, "PROMPT", max_tokens=42) == "out"
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["json"] == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "PROMPT"}],
        "max_tokens": 42,
        "temperature": 0.1,
    }


def test_registry_covers_all_harness_choices() -> None:
    from ordbokene.settings import HARNESS_CHOICES

    assert set(PROVIDERS) == set(HARNESS_CHOICES)
    assert set(HARNESSES) == set(HARNESS_CHOICES) - {"openrouter"}


def test_codex_argv_read_only_stdin_and_output_file() -> None:
    harness = HARNESSES["codex"]
    assert harness.input_mode == InputMode.STDIN
    assert harness.output_mode == OutputMode.FILE
    argv = harness.build_argv(LlmConfig(model="gpt-x"), output_path="/tmp/o.txt", prompt_path=None)
    assert argv[:2] == ["codex", "exec"]
    assert "--model" in argv and "gpt-x" in argv
    assert "--sandbox" in argv and "read-only" in argv
    assert "--skip-git-repo-check" in argv  # temp cwd is not a git repo
    assert argv[-1] == "-"  # reads prompt from stdin
    assert "--output-last-message" in argv and "/tmp/o.txt" in argv


def test_codex_argv_includes_reasoning_effort() -> None:
    argv = HARNESSES["codex"].build_argv(
        LlmConfig(model="m", reasoning_effort="high"), output_path="/tmp/o", prompt_path=None
    )
    assert "model_reasoning_effort=high" in argv


def test_claude_argv_stdin_stdout() -> None:
    harness = HARNESSES["claude"]
    assert (harness.input_mode, harness.output_mode) == (InputMode.STDIN, OutputMode.STDOUT)
    argv = harness.build_argv(LlmConfig(model="claude-x"), output_path=None, prompt_path=None)
    assert argv[:2] == ["claude", "-p"]
    assert "--model" in argv and "claude-x" in argv
    assert "--output-format" in argv and "text" in argv
    assert "--disallowedTools" in argv  # no tool use in a gloss completion


def test_opencode_argv_arg_input() -> None:
    harness = HARNESSES["opencode"]
    assert harness.input_mode == InputMode.ARG
    argv = harness.build_argv(
        LlmConfig(model="anthropic/claude", reasoning_effort="high"),
        output_path=None,
        prompt_path=None,
    )
    assert argv[:2] == ["opencode", "run"]
    assert "--pure" in argv  # no external plugins
    assert "anthropic/claude" in argv
    assert "--variant" in argv and "high" in argv


def test_droid_argv_reads_prompt_file() -> None:
    harness = HARNESSES["droid"]
    assert harness.input_mode == InputMode.FILE
    argv = harness.build_argv(
        LlmConfig(model="claude-opus", reasoning_effort="medium"),
        output_path=None,
        prompt_path="/tmp/p.txt",
    )
    assert argv[:2] == ["droid", "exec"]
    assert "-f" in argv and "/tmp/p.txt" in argv
    assert "-m" in argv and "claude-opus" in argv
    assert "-r" in argv and "medium" in argv


def test_pi_argv_disables_tools_and_context() -> None:
    argv = HARNESSES["pi"].build_argv(
        LlmConfig(model="google/gemini"), output_path=None, prompt_path=None
    )
    assert argv[0] == "pi"
    assert "-p" in argv and "--no-tools" in argv and "--no-context-files" in argv
    assert "--model" in argv and "google/gemini" in argv


def test_complete_cli_stdout_success(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        assert kwargs["cwd"]  # runs in a temp dir, not the repo
        return SimpleNamespace(returncode=0, stdout='{"1": {}}', stderr="")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    assert complete_cli(HARNESSES["claude"], LlmConfig(model="m", retry_delay=0), "prompt") == '{"1": {}}'


def test_complete_cli_file_output_success(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        out = argv[argv.index("--output-last-message") + 1]
        with open(out, "w", encoding="utf-8") as fh:
            fh.write('{"2": {}}')
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    assert complete_cli(HARNESSES["codex"], LlmConfig(model="m", retry_delay=0), "prompt") == '{"2": {}}'


def test_complete_cli_arg_input_appends_prompt(monkeypatch) -> None:
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    complete_cli(HARNESSES["opencode"], LlmConfig(model="m", retry_delay=0), "MY PROMPT")
    assert seen["argv"][-1] == "MY PROMPT"


def test_complete_cli_quota_raises_immediately(monkeypatch) -> None:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout="", stderr="You exceeded your usage limit")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    with pytest.raises(LlmError) as excinfo:
        complete_cli(HARNESSES["codex"], LlmConfig(model="m", max_retries=3, retry_delay=0), "p")
    assert excinfo.value.code == "codex_quota"
    assert len(calls) == 1  # no retry burn on quota


def test_complete_cli_nonzero_exit_exhausts_retries(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr="boom")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    with pytest.raises(LlmError) as excinfo:
        complete_cli(HARNESSES["droid"], LlmConfig(model="m", max_retries=2, retry_delay=0), "p")
    assert excinfo.value.code == "droid_exit_2"


def test_complete_cli_empty_output_raises(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="   ", stderr="")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    with pytest.raises(LlmError) as excinfo:
        complete_cli(HARNESSES["pi"], LlmConfig(model="m", max_retries=1, retry_delay=0), "p")
    assert excinfo.value.code == "pi_empty_response"


def test_cli_provider_ignores_session_and_max_tokens(monkeypatch) -> None:
    def fake_run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    provider = PROVIDERS["claude"]
    assert provider(None, LlmConfig(model="m", retry_delay=0), "p", max_tokens=999) == "{}"


def test_complete_cli_missing_binary_fails_fast(monkeypatch) -> None:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        raise FileNotFoundError("no such binary")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    with pytest.raises(LlmError) as excinfo:
        complete_cli(HARNESSES["codex"], LlmConfig(model="m", max_retries=3, retry_delay=0), "p")
    assert excinfo.value.code == "codex_error"
    assert len(calls) == 1  # missing binary: no retry burn


def test_complete_cli_quota_on_clean_exit(monkeypatch) -> None:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="You exceeded your usage limit")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    with pytest.raises(LlmError) as excinfo:
        complete_cli(HARNESSES["pi"], LlmConfig(model="m", max_retries=3, retry_delay=0), "p")
    assert excinfo.value.code == "pi_quota"
    assert len(calls) == 1  # quota on a clean exit still short-circuits


def test_complete_cli_arg_prompt_too_large() -> None:
    with pytest.raises(LlmError) as excinfo:
        complete_cli(HARNESSES["opencode"], LlmConfig(model="m", retry_delay=0), "x" * 200_000)
    assert excinfo.value.code == "opencode_prompt_too_large"


def test_complete_cli_closes_stdin_for_arg_mode(monkeypatch) -> None:
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    complete_cli(HARNESSES["opencode"], LlmConfig(model="m", retry_delay=0), "p")
    assert seen.get("stdin") is llm.subprocess.DEVNULL
    assert "input" not in seen


def test_complete_cli_passes_prompt_on_stdin_for_stdin_mode(monkeypatch) -> None:
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    complete_cli(HARNESSES["claude"], LlmConfig(model="m", retry_delay=0), "PROMPT")
    assert seen.get("input") == "PROMPT"
    assert "stdin" not in seen
