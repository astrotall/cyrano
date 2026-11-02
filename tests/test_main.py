from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cyrano import main as main_module
from cyrano.bot.errors import BotTokenRejected, PollingFailed
from cyrano.config import Settings, load_settings
from cyrano.domain.enums import ContactStatus
from cyrano.domain.models import ContactProfile
from cyrano.logging import configure_logging
from cyrano.main import main, run
from cyrano.userbot.errors import Disconnected, SessionMissing, SessionNotAuthorized
from cyrano.userbot.protocols import UserbotClient
from tests.bot.fakes import fake_control_bot_class
from tests.conftest import (
    OPTIONAL_ENV,
    PERSONAL_SETTING_NAMES,
    PERSONAL_VALUES,
    REQUIRED_ENV,
    SECRET_VALUES,
)
from tests.fake_app import CONNECTING_MARKER, HANGING_CONNECT_SCENARIO
from tests.services.fakes import FakeCardSender, InMemoryModes
from tests.userbot.fakes import (
    FAKE_SENDER_ID,
    FakeClient,
    FakeEvent,
    FakeSender,
    InMemoryContacts,
    InMemoryMessages,
    touch_session_file,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SIGNAL_EXIT_LIMIT_SECONDS = 2.0


@pytest.fixture
def session_env(
    full_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    session_path = tmp_path / "session" / "userbot"
    touch_session_file(tmp_path / "session" / "userbot.session")
    monkeypatch.setenv("USERBOT_SESSION_PATH", str(session_path))
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    return {**full_env, "USERBOT_SESSION_PATH": str(session_path)}


@dataclass
class Repositories:
    contacts: InMemoryContacts
    messages: InMemoryMessages
    modes: InMemoryModes


@pytest.fixture(autouse=True)
def repositories(monkeypatch: pytest.MonkeyPatch) -> Repositories:
    stored = Repositories(InMemoryContacts(), InMemoryMessages(), InMemoryModes())
    monkeypatch.setattr("cyrano.main.SqlContactRepository", lambda _: stored.contacts)
    monkeypatch.setattr("cyrano.main.SqlMessageRepository", lambda _: stored.messages)
    monkeypatch.setattr("cyrano.main.SqlModeRepository", lambda _: stored.modes)
    return stored


@pytest.fixture(autouse=True)
def bot_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr("cyrano.main.ControlBot", fake_control_bot_class(calls))
    return calls


def _factory(client: FakeClient) -> Callable[[Settings], UserbotClient]:
    def build(_: Settings) -> UserbotClient:
        return client

    return build


async def test_run_logs_startup_and_stops_on_event(
    session_env: dict[str, str], heartbeat_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = load_settings(env_file=None)
    configure_logging("INFO")
    stop = asyncio.Event()

    task = asyncio.create_task(run(settings, stop, client_factory=_factory(FakeClient())))
    await asyncio.sleep(0.05)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    out = capsys.readouterr().out
    records = [json.loads(line) for line in out.splitlines() if line]
    assert [r["msg"] for r in records] == [
        "cyrano started",
        "userbot listening",
        "cyrano shutting down",
        "userbot stopped",
        "cyrano stopped",
    ]
    for secret in (*SECRET_VALUES, *PERSONAL_VALUES):
        assert secret not in out
    for name in PERSONAL_SETTING_NAMES:
        assert name not in records[0]


def _spawn(env: dict[str, str], *scenario: str) -> subprocess.Popen[str]:
    full = {**os.environ, **env}
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "tests.fake_app", *scenario],
        env=full,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _healthcheck(path: Path) -> int:
    cmd = [sys.executable, "-m", "cyrano.health", "--path", str(path)]
    return subprocess.run(cmd, capture_output=True, check=False).returncode  # noqa: S603


def test_process_exits_cleanly_on_sigterm(
    session_env: dict[str, str], heartbeat_file: Path
) -> None:
    proc = _spawn({**session_env, "HEARTBEAT_PATH": str(heartbeat_file)})
    assert proc.stdout is not None
    first = proc.stdout.readline()
    assert json.loads(first)["msg"] == "cyrano started"
    for value in PERSONAL_VALUES:
        assert value not in first
    for _ in range(50):
        if _healthcheck(heartbeat_file) == 0:
            break
        time.sleep(0.1)
    else:
        pytest.fail("healthcheck never reported the running app as alive")

    proc.send_signal(signal.SIGTERM)
    stdout, _ = proc.communicate(timeout=10)

    assert proc.returncode == 0
    assert "cyrano stopped" in stdout
    assert not _exists(heartbeat_file)
    assert _healthcheck(heartbeat_file) == 1


def test_process_exits_quickly_and_cleanly_on_sigterm_while_connecting(
    session_env: dict[str, str], heartbeat_file: Path
) -> None:
    proc = _spawn({**session_env, "HEARTBEAT_PATH": str(heartbeat_file)}, HANGING_CONNECT_SCENARIO)
    assert proc.stdout is not None
    lines: list[str] = []
    while (line := proc.stdout.readline()).strip() != CONNECTING_MARKER:
        assert line, "the app ended before it started connecting"
        lines.append(line)

    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    stdout, stderr = proc.communicate(timeout=SIGNAL_EXIT_LIMIT_SECONDS)
    elapsed = time.monotonic() - started

    assert elapsed < SIGNAL_EXIT_LIMIT_SECONDS
    assert proc.returncode == 0
    assert "Traceback" not in stderr
    assert "cyrano stopped" in "".join(lines) + stdout
    assert not _exists(heartbeat_file)


def test_process_fails_fast_without_config() -> None:
    env = {k: v for k, v in os.environ.items() if k not in {*REQUIRED_ENV, *OPTIONAL_ENV}}
    proc = subprocess.run(
        [sys.executable, "-m", "cyrano"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        cwd="/",
        check=False,
    )

    assert proc.returncode == 2
    assert "Missing required environment variables" in proc.stderr
    assert "BOT_TOKEN" in proc.stderr
    assert "Traceback" not in proc.stderr


def _mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns


def _exists(path: Path) -> bool:
    return path.exists()


class _EngineSpy:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


async def test_run_builds_the_engine_and_disposes_it_on_stop(
    session_env: dict[str, str], heartbeat_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings(env_file=None)
    engine = _EngineSpy()
    built_from: list[Settings] = []

    def build(from_settings: Settings) -> _EngineSpy:
        built_from.append(from_settings)
        return engine

    monkeypatch.setattr("cyrano.main.build_engine", build)
    stop = asyncio.Event()

    task = asyncio.create_task(run(settings, stop, client_factory=_factory(FakeClient())))
    await asyncio.sleep(0.05)
    assert built_from == [settings]
    assert engine.disposed is False
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert engine.disposed is True


async def test_run_keeps_touching_heartbeat_and_removes_it_on_stop(
    session_env: dict[str, str], heartbeat_file: Path
) -> None:
    settings = load_settings(env_file=None)
    stop = asyncio.Event()

    task = asyncio.create_task(
        run(settings, stop, heartbeat_interval=0.02, client_factory=_factory(FakeClient()))
    )
    await asyncio.sleep(0.1)
    first = _mtime_ns(heartbeat_file)
    await asyncio.sleep(0.1)
    assert _mtime_ns(heartbeat_file) > first
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert not _exists(heartbeat_file)


async def test_shutdown_removes_the_handler_then_disconnects_then_disposes_the_engine(
    session_env: dict[str, str], heartbeat_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class _Engine:
        async def dispose(self) -> None:
            calls.append("engine.dispose")

    monkeypatch.setattr("cyrano.main.build_engine", lambda _: _Engine())
    settings = load_settings(env_file=None)
    stop = asyncio.Event()

    task = asyncio.create_task(
        run(settings, stop, client_factory=_factory(FakeClient(calls=calls)))
    )
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert calls == [
        "add_event_handler",
        "connect",
        "is_user_authorized",
        "catch_up",
        "remove_event_handler",
        "disconnect",
        "engine.dispose",
    ]


async def test_stop_signal_while_connecting_cleans_up_without_an_error(
    session_env: dict[str, str], heartbeat_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class _Engine:
        async def dispose(self) -> None:
            calls.append("engine.dispose")

    monkeypatch.setattr("cyrano.main.build_engine", lambda _: _Engine())
    client = FakeClient(hang_on_connect=True, calls=calls)
    settings = load_settings(env_file=None)
    stop = asyncio.Event()

    task = asyncio.create_task(run(settings, stop, client_factory=_factory(client)))
    await asyncio.sleep(0.05)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert calls == [
        "add_event_handler",
        "connect",
        "remove_event_handler",
        "disconnect",
        "engine.dispose",
    ]
    assert client.write_attempts == []
    assert not _exists(heartbeat_file)


async def test_run_without_a_session_file_fails_before_any_client_is_built(
    full_env: dict[str, str], heartbeat_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERBOT_SESSION_PATH", str(tmp_path / "nothing" / "userbot"))
    settings = load_settings(env_file=None)
    built: list[Settings] = []

    def build(from_settings: Settings) -> UserbotClient:
        built.append(from_settings)
        return FakeClient()

    with pytest.raises(SessionMissing, match="make login"):
        await run(settings, asyncio.Event(), client_factory=build)

    assert built == []
    assert not _exists(heartbeat_file)
    assert not _exists(tmp_path / "nothing")


async def test_run_with_an_unauthorized_session_fails_and_cleans_up(
    session_env: dict[str, str], heartbeat_file: Path
) -> None:
    client = FakeClient(authorized=False)
    settings = load_settings(env_file=None)

    with pytest.raises(SessionNotAuthorized, match="make login"):
        await run(settings, asyncio.Event(), client_factory=_factory(client))

    assert "catch_up" not in client.calls
    assert client.calls[-2:] == ["remove_event_handler", "disconnect"]
    assert not _exists(heartbeat_file)


async def test_run_fails_when_the_connection_is_lost(
    session_env: dict[str, str], heartbeat_file: Path
) -> None:
    client = FakeClient()
    settings = load_settings(env_file=None)

    task = asyncio.create_task(run(settings, asyncio.Event(), client_factory=_factory(client)))
    await asyncio.sleep(0.05)
    client.lose_connection()

    with pytest.raises(Disconnected):
        await asyncio.wait_for(task, timeout=2)

    assert client.calls[-2:] == ["remove_event_handler", "disconnect"]
    assert not _exists(heartbeat_file)


def test_process_without_a_session_exits_with_a_readable_message(
    full_env: dict[str, str], tmp_path: Path
) -> None:
    env = {**os.environ, **full_env, "USERBOT_SESSION_PATH": str(tmp_path / "x" / "userbot")}
    proc = subprocess.run(
        [sys.executable, "-m", "cyrano"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        cwd="/",
        check=False,
    )

    assert proc.returncode == 1
    assert "make login" in proc.stderr
    assert "Traceback" not in proc.stderr
    assert proc.stdout == ""
    assert not (tmp_path / "x").exists()


def test_login_subcommand_runs_the_login_flow(
    full_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    ran: list[Settings] = []

    async def fake_login(settings: Settings) -> None:
        ran.append(settings)

    monkeypatch.setattr(main_module, "run_login", fake_login)

    assert main(["login"]) == 0
    assert len(ran) == 1


def test_login_cancelled_at_a_prompt_is_a_readable_exit(
    full_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    async def cancelled(settings: Settings) -> None:
        raise EOFError

    monkeypatch.setattr(main_module, "run_login", cancelled)

    assert main(["login"]) == 1
    assert "Nothing was saved" in capsys.readouterr().err


def test_unknown_subcommand_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["unknown"])

    assert raised.value.code == 2


POLL_ATTEMPTS = 200
POLL_INTERVAL_SECONDS = 0.01


async def _wait_until(condition: Callable[[], Awaitable[bool]]) -> None:
    for _ in range(POLL_ATTEMPTS):
        if await condition():
            return
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail("the condition was never met")


async def _collected(repositories: Repositories, tg_id: int) -> bool:
    contact = await repositories.contacts.get_by_tg_id(tg_id)
    return (
        contact is not None
        and ContactProfile.model_validate(contact.profile).collected_at is not None
    )


def _readable_client(**kwargs: object) -> FakeClient:
    client = FakeClient(**kwargs)  # type: ignore[arg-type]
    client.users[FAKE_SENDER_ID] = FakeSender()
    client.about = "fake bio"
    return client


async def test_the_collector_is_started_and_sweeps_waiting_contacts(
    session_env: dict[str, str], heartbeat_file: Path, repositories: Repositories
) -> None:
    await repositories.contacts.seed(FAKE_SENDER_ID)
    client = _readable_client()
    stop = asyncio.Event()

    task = asyncio.create_task(
        run(load_settings(env_file=None), stop, client_factory=_factory(client))
    )
    await _wait_until(lambda: _collected(repositories, FAKE_SENDER_ID))
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert client.write_attempts == []
    assert client.calls.index("catch_up") < client.calls.index("get_entity")


async def test_a_new_contact_registered_by_the_intake_is_collected(
    session_env: dict[str, str], heartbeat_file: Path, repositories: Repositories
) -> None:
    client = _readable_client()
    stop = asyncio.Event()
    task = asyncio.create_task(
        run(load_settings(env_file=None), stop, client_factory=_factory(client))
    )
    await asyncio.sleep(0.05)

    await client.deliver(FakeEvent())
    await _wait_until(lambda: _collected(repositories, FAKE_SENDER_ID))
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    contact = await repositories.contacts.get_by_tg_id(FAKE_SENDER_ID)
    assert contact is not None
    assert len(repositories.messages.stored) == 1
    assert client.write_attempts == []


async def test_the_collector_is_cancelled_before_the_client_disconnects(
    session_env: dict[str, str], heartbeat_file: Path, repositories: Repositories
) -> None:
    await repositories.contacts.seed(FAKE_SENDER_ID)
    calls: list[str] = []
    client = _readable_client(hang_on_read=True, calls=calls)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run(load_settings(env_file=None), stop, client_factory=_factory(client))
    )

    await _wait_until(lambda: _contains(calls, "get_entity"))
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert calls.index("get_entity cancelled") < calls.index("remove_event_handler")
    assert calls.index("remove_event_handler") < calls.index("disconnect")
    assert not await _collected(repositories, FAKE_SENDER_ID)


async def _contains(calls: list[str], name: str) -> bool:
    return name in calls


async def test_a_crashing_collector_does_not_stop_the_intake(
    session_env: dict[str, str],
    heartbeat_file: Path,
    repositories: Repositories,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _CrashingCollector:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def run(self) -> None:
            raise RuntimeError("fake collector crash")

    monkeypatch.setattr("cyrano.main.ProfileCollector", _CrashingCollector)
    configure_logging("INFO")
    client = _readable_client()
    stop = asyncio.Event()
    task = asyncio.create_task(
        run(load_settings(env_file=None), stop, client_factory=_factory(client))
    )
    await asyncio.sleep(0.05)

    await client.deliver(FakeEvent())
    await asyncio.sleep(0.05)

    assert not task.done()
    assert len(repositories.messages.stored) == 1
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    crashes = [r for r in records if r["msg"] == "profile collector stopped"]
    assert [r["error"] for r in crashes] == ["RuntimeError"]
    assert "fake collector crash" not in json.dumps(records)


async def test_the_collector_is_not_started_when_the_start_is_interrupted(
    session_env: dict[str, str],
    heartbeat_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[object] = []

    class _RecordingCollector:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created.append(self)

        async def run(self) -> None:
            created.append("run")

    monkeypatch.setattr("cyrano.main.ProfileCollector", _RecordingCollector)
    client = FakeClient(hang_on_connect=True)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)

    await asyncio.wait_for(
        run(load_settings(env_file=None), stop, client_factory=_factory(client)), timeout=2
    )

    assert "run" not in created
    assert client.write_attempts == []


class _Announcer:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def run(self) -> None:
        self.calls.append("announcer.run")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.calls.append("announcer cancelled")
            raise


def _recording_announcer(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    monkeypatch.setattr("cyrano.main.CardAnnouncer", lambda *args, **kwargs: _Announcer(calls))


async def test_the_bot_and_the_announcer_start_only_after_the_userbot_is_ready(
    session_env: dict[str, str],
    heartbeat_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    bot_calls: list[str],
) -> None:
    _recording_announcer(monkeypatch, bot_calls)
    client = FakeClient(calls=bot_calls)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run(load_settings(env_file=None), stop, client_factory=_factory(client))
    )

    await _wait_until(lambda: _contains(bot_calls, "bot.poll"))
    await _wait_until(lambda: _contains(bot_calls, "announcer.run"))
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert bot_calls.index("catch_up") < bot_calls.index("bot.poll")
    assert bot_calls.index("catch_up") < bot_calls.index("announcer.run")
    assert client.write_attempts == []


async def test_the_announcer_announces_a_waiting_contact_through_the_bot_sender(
    session_env: dict[str, str],
    heartbeat_file: Path,
    repositories: Repositories,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = FakeCardSender()
    monkeypatch.setattr("cyrano.main.ControlBot", fake_control_bot_class(sender=sender))
    contact = await repositories.contacts.seed(
        FAKE_SENDER_ID,
        profile=ContactProfile(collected_at=datetime.now(UTC)).model_dump(mode="json"),
    )
    stop = asyncio.Event()
    task = asyncio.create_task(
        run(load_settings(env_file=None), stop, client_factory=_factory(FakeClient()))
    )

    await _wait_until(lambda: _status_is(repositories, contact.tg_id, ContactStatus.PENDING))
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    assert [card.contact_id for card in sender.cards] == [contact.id]


async def _status_is(repositories: Repositories, tg_id: int, status: ContactStatus) -> bool:
    contact = await repositories.contacts.get_by_tg_id(tg_id)
    return contact is not None and contact.status is status


async def test_shutdown_cancels_the_announcer_then_stops_the_bot_then_the_collector_and_userbot(
    session_env: dict[str, str],
    heartbeat_file: Path,
    repositories: Repositories,
    monkeypatch: pytest.MonkeyPatch,
    bot_calls: list[str],
) -> None:
    _recording_announcer(monkeypatch, bot_calls)
    await repositories.contacts.seed(FAKE_SENDER_ID)
    client = _readable_client(hang_on_read=True, calls=bot_calls)
    stop = asyncio.Event()
    task = asyncio.create_task(
        run(load_settings(env_file=None), stop, client_factory=_factory(client))
    )

    await _wait_until(lambda: _contains(bot_calls, "get_entity"))
    await _wait_until(lambda: _contains(bot_calls, "bot.poll"))
    stop.set()
    await asyncio.wait_for(task, timeout=2)

    order = [
        "announcer cancelled",
        "bot.stop",
        "bot.poll cancelled",
        "get_entity cancelled",
        "remove_event_handler",
        "disconnect",
    ]
    assert [bot_calls.index(name) for name in order] == sorted(bot_calls.index(n) for n in order)


async def test_neither_the_bot_nor_the_announcer_starts_when_the_start_is_interrupted(
    session_env: dict[str, str],
    heartbeat_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    bot_calls: list[str],
) -> None:
    _recording_announcer(monkeypatch, bot_calls)
    client = FakeClient(hang_on_connect=True, calls=bot_calls)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)

    await asyncio.wait_for(
        run(load_settings(env_file=None), stop, client_factory=_factory(client)), timeout=2
    )

    assert "bot.poll" not in bot_calls
    assert "announcer.run" not in bot_calls
    assert bot_calls.count("bot.stop") == 1


async def test_a_polling_failure_ends_the_app_with_a_readable_error(
    session_env: dict[str, str],
    heartbeat_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    bot_calls: list[str],
) -> None:
    monkeypatch.setattr(
        "cyrano.main.ControlBot", fake_control_bot_class(bot_calls, poll_error=RuntimeError("boom"))
    )
    client = FakeClient(calls=bot_calls)

    with pytest.raises(PollingFailed, match="RuntimeError"):
        await asyncio.wait_for(
            run(load_settings(env_file=None), asyncio.Event(), client_factory=_factory(client)),
            timeout=2,
        )

    assert bot_calls[-2:] == ["remove_event_handler", "disconnect"]
    assert "bot.stop" in bot_calls
    assert not _exists(heartbeat_file)


async def test_a_rejected_token_during_polling_ends_the_app_with_that_error(
    session_env: dict[str, str], heartbeat_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "cyrano.main.ControlBot", fake_control_bot_class(poll_error=BotTokenRejected())
    )

    with pytest.raises(BotTokenRejected, match="BOT_TOKEN"):
        await asyncio.wait_for(
            run(
                load_settings(env_file=None),
                asyncio.Event(),
                client_factory=_factory(FakeClient()),
            ),
            timeout=2,
        )


async def test_polling_that_ends_without_a_request_is_a_failure(
    session_env: dict[str, str], heartbeat_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cyrano.main.ControlBot", fake_control_bot_class(poll_returns=True))

    with pytest.raises(PollingFailed, match="unexpectedly"):
        await asyncio.wait_for(
            run(
                load_settings(env_file=None),
                asyncio.Event(),
                client_factory=_factory(FakeClient()),
            ),
            timeout=2,
        )


async def test_a_bot_that_cannot_be_built_is_a_readable_error_before_anything_connects(
    session_env: dict[str, str], heartbeat_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object) -> object:
        raise BotTokenRejected

    monkeypatch.setattr("cyrano.main.ControlBot", refuse)
    client = FakeClient()

    with pytest.raises(BotTokenRejected):
        await run(load_settings(env_file=None), asyncio.Event(), client_factory=_factory(client))

    assert client.calls == []
    assert not _exists(heartbeat_file)


async def test_a_crashing_announcer_is_logged_once_and_does_not_stop_the_intake(
    session_env: dict[str, str],
    heartbeat_file: Path,
    repositories: Repositories,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _CrashingAnnouncer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def run(self) -> None:
            raise RuntimeError("fake announcer crash")

    monkeypatch.setattr("cyrano.main.CardAnnouncer", _CrashingAnnouncer)
    configure_logging("INFO")
    client = _readable_client()
    stop = asyncio.Event()
    task = asyncio.create_task(
        run(load_settings(env_file=None), stop, client_factory=_factory(client))
    )
    await asyncio.sleep(0.05)

    await client.deliver(FakeEvent())
    await asyncio.sleep(0.05)

    assert not task.done()
    assert len(repositories.messages.stored) == 1
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    crashes = [r for r in records if r["msg"] == "card announcer stopped"]
    assert [r["error"] for r in crashes] == ["RuntimeError"]
    assert "fake announcer crash" not in json.dumps(records)


async def test_a_polling_failure_is_logged_with_the_class_name_only(
    session_env: dict[str, str],
    heartbeat_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "cyrano.main.ControlBot", fake_control_bot_class(poll_error=RuntimeError("secret detail"))
    )
    configure_logging("INFO")

    with pytest.raises(PollingFailed):
        await asyncio.wait_for(
            run(
                load_settings(env_file=None),
                asyncio.Event(),
                client_factory=_factory(FakeClient()),
            ),
            timeout=2,
        )

    out = capsys.readouterr().out
    assert "control bot polling failed" in out
    assert "secret detail" not in out
    for secret in (*SECRET_VALUES, *PERSONAL_VALUES):
        assert secret not in out


@pytest.mark.parametrize("error", [BotTokenRejected(), PollingFailed("RuntimeError")])
def test_a_control_bot_error_is_a_readable_exit(
    error: Exception,
    full_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    async def failing(settings: Settings) -> None:
        raise error

    monkeypatch.setattr(main_module, "run", failing)

    assert main([]) == 1
    assert str(error) in capsys.readouterr().err
