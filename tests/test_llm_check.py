from __future__ import annotations

import httpx
import pytest

from cyrano import main as main_module
from cyrano.config import Settings
from cyrano.domain.errors import LLMAuthError, LLMUnavailableError
from cyrano.llm.factory import LLMClientFactory
from cyrano.main import main, run_llm_check
from tests.llm.fakes import Script, completion_response, error_response
from tests.services.fakes import FAKE_LLM_REPLY, FakeLLMClient


async def test_success_prints_one_line_with_model_tokens_and_latency(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = await run_llm_check(FakeLLMClient(), "fake-model")

    captured = capsys.readouterr()
    assert exit_code == 0
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("llm-check ok: model=fake-model input_tokens=11 output_tokens=2")
    assert "latency_ms=" in lines[0]
    assert "cache_hit_tokens" not in lines[0]
    assert captured.err == ""


async def test_success_never_prints_the_reply_text(capsys: pytest.CaptureFixture[str]) -> None:
    await run_llm_check(FakeLLMClient(), "fake-model")

    captured = capsys.readouterr()
    assert FAKE_LLM_REPLY not in captured.out + captured.err


async def test_failure_prints_a_readable_line_and_returns_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = LLMAuthError("LLM provider refused the credentials (HTTP 401)", status_code=401)

    exit_code = await run_llm_check(FakeLLMClient(error=error), "fake-model")

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.strip() == (
        "llm-check failed: LLMAuthError: LLM provider refused the credentials (HTTP 401)"
    )


async def test_failure_without_a_status_is_readable_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = LLMUnavailableError("LLM provider unreachable after 3 retries (ReadTimeout)")

    exit_code = await run_llm_check(FakeLLMClient(error=error), "fake-model")

    assert exit_code == 1
    assert "LLMUnavailableError" in capsys.readouterr().err


def _patch_factory(monkeypatch: pytest.MonkeyPatch, script: Script) -> None:
    class MockedFactory(LLMClientFactory):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings, http_transport=httpx.MockTransport(script))

    monkeypatch.setattr(main_module, "LLMClientFactory", MockedFactory)


def test_the_command_runs_through_main_with_a_mocked_transport(
    full_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir("/")
    script = Script(completion_response("OK", cache_hit_tokens=3))
    _patch_factory(monkeypatch, script)

    exit_code = main(["llm-check"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "llm-check ok: model=deepseek-chat input_tokens=12 output_tokens=7" in captured.out
    assert "cache_hit_tokens=3" in captured.out
    assert script.body()["max_tokens"] == 5


def test_the_command_exits_one_on_a_rejected_key(
    full_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir("/")
    _patch_factory(monkeypatch, Script(error_response(401)))

    exit_code = main(["llm-check"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "LLMAuthError" in captured.err
    assert "401" in captured.err
    assert "sk-deepseek-secret" not in captured.out + captured.err


def test_the_command_needs_a_valid_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir("/")

    assert main(["llm-check"]) == 2
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err
