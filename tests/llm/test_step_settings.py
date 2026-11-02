from __future__ import annotations

import pytest

from cyrano.config import ConfigError, load_settings
from cyrano.domain.enums import LLMStep
from tests.llm.fakes import FakeSleep, Script, completion_response, conversation, factory_for


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)


async def test_reply_and_summary_clients_use_their_own_settings(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, sleep: FakeSleep
) -> None:
    _env(
        monkeypatch,
        LLM_REPLY_MODEL="reply-model",
        LLM_SUMMARY_MODEL="summary-model",
        LLM_REPLY_TEMPERATURE="0.7",
        LLM_SUMMARY_TEMPERATURE="0.1",
        LLM_REPLY_MAX_TOKENS="123",
        LLM_SUMMARY_MAX_TOKENS="456",
    )
    script = Script(completion_response(), repeat_last=True)
    factory = factory_for(load_settings(env_file=None), script, sleep)

    await factory.get(LLMStep.REPLY).complete(conversation())
    await factory.get(LLMStep.SUMMARY).complete(conversation())
    await factory.aclose()

    assert (
        script.body(0)["model"],
        script.body(0)["temperature"],
        script.body(0)["max_tokens"],
    ) == (
        "reply-model",
        0.7,
        123,
    )
    assert (
        script.body(1)["model"],
        script.body(1)["temperature"],
        script.body(1)["max_tokens"],
    ) == (
        "summary-model",
        0.1,
        456,
    )


async def test_the_defaults_differ_per_step_and_fall_back_to_deepseek_model(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, sleep: FakeSleep
) -> None:
    _env(monkeypatch, DEEPSEEK_MODEL="shared-model")
    script = Script(completion_response(), repeat_last=True)
    factory = factory_for(load_settings(env_file=None), script, sleep)

    await factory.get(LLMStep.REPLY).complete(conversation())
    await factory.get(LLMStep.SUMMARY).complete(conversation())
    await factory.aclose()

    assert script.body(0)["model"] == script.body(1)["model"] == "shared-model"
    assert (script.body(0)["temperature"], script.body(0)["max_tokens"]) == (1.0, 400)
    assert (script.body(1)["temperature"], script.body(1)["max_tokens"]) == (0.2, 1500)


async def test_only_one_step_model_overridden_leaves_the_other_on_the_fallback(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, sleep: FakeSleep
) -> None:
    _env(monkeypatch, LLM_SUMMARY_MODEL="summary-model")
    script = Script(completion_response(), repeat_last=True)
    factory = factory_for(load_settings(env_file=None), script, sleep)

    await factory.get(LLMStep.REPLY).complete(conversation())
    await factory.get(LLMStep.SUMMARY).complete(conversation())
    await factory.aclose()

    assert script.body(0)["model"] == "deepseek-chat"
    assert script.body(1)["model"] == "summary-model"


async def test_per_call_arguments_override_the_step_defaults(
    full_env: dict[str, str], sleep: FakeSleep
) -> None:
    script = Script(completion_response())
    factory = factory_for(load_settings(env_file=None), script, sleep)

    await factory.get(LLMStep.REPLY).complete(conversation(), temperature=0.0, max_tokens=9)
    await factory.aclose()

    assert script.body()["temperature"] == 0.0
    assert script.body()["max_tokens"] == 9


async def test_the_client_knows_its_step_and_model(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, sleep: FakeSleep
) -> None:
    _env(monkeypatch, LLM_REPLY_MODEL="reply-model")
    factory = factory_for(load_settings(env_file=None), Script(completion_response()), sleep)

    client = factory.get(LLMStep.REPLY)
    await factory.aclose()

    assert client.step is LLMStep.REPLY
    assert client.model == "reply-model"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("LLM_REPLY_TEMPERATURE", "2.5"),
        ("LLM_REPLY_TEMPERATURE", "-0.1"),
        ("LLM_SUMMARY_TEMPERATURE", "3"),
        ("LLM_SUMMARY_TEMPERATURE", "warm-fake-value"),
        ("LLM_REPLY_MAX_TOKENS", "0"),
        ("LLM_REPLY_MAX_TOKENS", "-5"),
        ("LLM_SUMMARY_MAX_TOKENS", "0"),
        ("LLM_SUMMARY_MAX_TOKENS", "many-fake-value"),
        ("LLM_REPLY_MODEL", "   "),
        ("LLM_SUMMARY_MODEL", "   "),
        ("DEEPSEEK_MODEL", "   "),
        ("DEEPSEEK_BASE_URL", "ftp://fake-host.test"),
        ("DEEPSEEK_BASE_URL", "fake-host.test/path"),
        ("DEEPSEEK_BASE_URL", "https://"),
        ("DEEPSEEK_THINKING", "maybe-fake-value"),
    ],
)
def test_nonsense_values_give_a_readable_error_that_does_not_echo_them(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError) as raised:
        load_settings(env_file=None)

    message = str(raised.value)
    assert f"  - {name}:" in message
    if "fake" in value:
        assert value not in message


def test_empty_values_count_as_unset(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, LLM_REPLY_MODEL="", LLM_REPLY_TEMPERATURE="", DEEPSEEK_BASE_URL="")

    settings = load_settings(env_file=None)

    assert settings.reply_model == "deepseek-chat"
    assert settings.llm_reply_temperature == 1.0
    assert settings.deepseek_base_url == "https://api.deepseek.com"


def test_the_defaults_are_documented_values(full_env: dict[str, str]) -> None:
    settings = load_settings(env_file=None)

    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.deepseek_thinking is False
    assert settings.llm_reply_model is None
    assert settings.llm_summary_model is None
    assert settings.llm_reply_temperature == 1.0
    assert settings.llm_summary_temperature == 0.2
    assert settings.llm_reply_max_tokens == 400
    assert settings.llm_summary_max_tokens == 1500


def test_the_boundary_values_are_accepted(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(
        monkeypatch,
        LLM_REPLY_TEMPERATURE="0",
        LLM_SUMMARY_TEMPERATURE="2",
        LLM_REPLY_MAX_TOKENS="1",
        DEEPSEEK_BASE_URL=" http://localhost:8080/v1 ",
    )

    settings = load_settings(env_file=None)

    assert settings.llm_reply_temperature == 0.0
    assert settings.llm_summary_temperature == 2.0
    assert settings.llm_reply_max_tokens == 1
    assert settings.deepseek_base_url == "http://localhost:8080/v1"


def test_the_startup_summary_has_the_step_settings_but_no_base_url_or_key(
    full_env: dict[str, str],
) -> None:
    summary = load_settings(env_file=None).safe_summary()

    assert summary["llm_reply_max_tokens"] == "400"
    assert summary["deepseek_thinking"] == "False"
    assert "deepseek_base_url" not in summary
    assert "sk-deepseek-secret" not in str(summary)
