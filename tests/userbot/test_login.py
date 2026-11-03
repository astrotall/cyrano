from __future__ import annotations

import logging
import stat
from pathlib import Path

import pytest
from telethon.errors import PhoneCodeInvalidError

from cyrano.config import Settings, load_settings
from cyrano.userbot.client import session_file
from cyrano.userbot.errors import ConnectFailed, LoginFailed
from cyrano.userbot.login import (
    ALREADY_AUTHORIZED_MESSAGE,
    SESSION_SAVED_MESSAGE,
    LoginPrompts,
    run_login,
)
from cyrano.userbot.protocols import LoginClient
from tests.privacy import FAKE_LOGIN_CODE, FAKE_PHONE, assert_absent
from tests.userbot.fakes import FakeLoginClient, touch_session_file

PHONE = FAKE_PHONE
CODE = FAKE_LOGIN_CODE
PASSWORD = "fake-two-step-password"


class _Recorder:
    def __init__(self) -> None:
        self.asked: list[str] = []
        self.prompts = LoginPrompts(phone=self._phone, code=self._code, password=self._password)

    def _phone(self) -> str:
        self.asked.append("phone")
        return f" {PHONE} "

    def _code(self) -> str:
        self.asked.append("code")
        return f" {CODE}\n"

    def _password(self) -> str:
        self.asked.append("password")
        return PASSWORD


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, full_env: dict[str, str]) -> Settings:
    monkeypatch.setenv("USERBOT_SESSION_PATH", str(tmp_path / "session" / "userbot"))
    return load_settings(env_file=None)


def _factory(client: FakeLoginClient, settings: Settings, *, creates_file: bool = True):  # type: ignore[no-untyped-def]
    def build(_: Settings) -> LoginClient:
        if creates_file:
            touch_session_file(session_file(settings.userbot_session_path))
        return client

    return build


async def test_login_asks_phone_and_code_and_saves_the_session(settings: Settings) -> None:
    client = FakeLoginClient()
    recorder = _Recorder()
    output: list[str] = []

    await run_login(settings, recorder.prompts, _factory(client, settings), output.append)

    assert recorder.asked == ["phone", "code"]
    assert client.sign_in_arguments == [(PHONE, CODE, None)]
    assert client.calls == ["connect", "send_code_request", "sign_in", "disconnect"]
    assert output == [SESSION_SAVED_MESSAGE]


async def test_two_step_password_is_asked_only_when_telegram_requires_it(
    settings: Settings,
) -> None:
    client = FakeLoginClient(password_required=True)
    recorder = _Recorder()

    await run_login(settings, recorder.prompts, _factory(client, settings), lambda _: None)

    assert recorder.asked == ["phone", "code", "password"]
    assert client.sign_in_arguments == [(PHONE, CODE, None), (None, None, PASSWORD)]


async def test_session_file_gets_mode_0600(settings: Settings) -> None:
    client = FakeLoginClient()
    file = session_file(settings.userbot_session_path)

    def build(_: Settings) -> LoginClient:
        touch_session_file(file)
        file.chmod(0o644)
        return client

    await run_login(settings, _Recorder().prompts, build, lambda _: None)

    assert stat.S_IMODE(file.stat().st_mode) == 0o600


async def test_phone_code_and_password_are_never_stored_or_logged(
    settings: Settings, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeLoginClient(password_required=True)
    recorder = _Recorder()

    with caplog.at_level(logging.DEBUG):
        await run_login(settings, recorder.prompts, _factory(client, settings), print)

    captured = capsys.readouterr()
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for secret in (PHONE, CODE, PASSWORD, PHONE.lstrip("+")):
        assert_absent(secret, captured.out + captured.err)
        assert_absent(secret, logged)
        assert_absent(secret, repr(settings))
        assert_absent(secret, repr(settings.model_dump()))


async def test_an_authorized_session_asks_no_code(
    settings: Settings,
) -> None:
    client = FakeLoginClient(authorized=True)
    recorder = _Recorder()
    output: list[str] = []
    file = session_file(settings.userbot_session_path)
    touch_session_file(file)
    file.chmod(0o644)

    await run_login(settings, recorder.prompts, _factory(client, settings), output.append)

    assert recorder.asked == ["phone"]
    assert client.sign_in_arguments == []
    assert output == [ALREADY_AUTHORIZED_MESSAGE]
    assert file.exists()
    assert stat.S_IMODE(file.stat().st_mode) == 0o600


async def test_a_wrong_code_fails_readably_and_leaves_no_session_file(
    settings: Settings,
) -> None:
    client = FakeLoginClient(code_error=PhoneCodeInvalidError(request=None))
    output: list[str] = []

    with pytest.raises(LoginFailed) as raised:
        await run_login(settings, _Recorder().prompts, _factory(client, settings), output.append)

    assert "PhoneCodeInvalidError" in str(raised.value)
    assert_absent(CODE, str(raised.value))
    assert output == []
    assert not session_file(settings.userbot_session_path).exists()
    assert client.calls[-1] == "disconnect"


async def test_a_failed_login_keeps_a_session_file_that_existed_before(
    settings: Settings,
) -> None:
    file = session_file(settings.userbot_session_path)
    touch_session_file(file)
    client = FakeLoginClient(code_error=PhoneCodeInvalidError(request=None))

    with pytest.raises(LoginFailed):
        await run_login(settings, _Recorder().prompts, _factory(client, settings), print)

    assert file.exists()


async def test_no_network_is_a_readable_error_and_leaves_no_session_file(
    settings: Settings,
) -> None:
    client = FakeLoginClient(connect_error=OSError("fake"))

    with pytest.raises(ConnectFailed):
        await run_login(settings, _Recorder().prompts, _factory(client, settings), print)

    assert not session_file(settings.userbot_session_path).exists()


async def test_aborting_at_the_phone_prompt_builds_no_client_and_makes_no_network_call(
    settings: Settings,
) -> None:
    built: list[Settings] = []

    def aborted() -> str:
        raise EOFError

    def build(from_settings: Settings) -> LoginClient:
        built.append(from_settings)
        return FakeLoginClient()

    prompts = LoginPrompts(phone=aborted, code=aborted, password=aborted)

    with pytest.raises(EOFError):
        await run_login(settings, prompts, build, print)

    assert built == []
    assert not session_file(settings.userbot_session_path).exists()


async def test_aborting_at_the_code_prompt_leaves_no_session_file(settings: Settings) -> None:
    client = FakeLoginClient()

    def aborted() -> str:
        raise EOFError

    prompts = LoginPrompts(phone=lambda: PHONE, code=aborted, password=aborted)

    with pytest.raises(EOFError):
        await run_login(settings, prompts, _factory(client, settings), print)

    assert not session_file(settings.userbot_session_path).exists()
    assert client.calls[-1] == "disconnect"
