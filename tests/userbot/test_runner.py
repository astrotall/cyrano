from __future__ import annotations

import asyncio

import pytest

from cyrano.userbot import client as client_module
from cyrano.userbot.errors import ConnectTimedOut, Disconnected, SessionNotAuthorized
from cyrano.userbot.runner import Userbot
from tests.userbot.fakes import FakeClient, FakeEvent, FakeIntake


async def test_start_registers_the_handler_connects_and_then_catches_up() -> None:
    client = FakeClient()

    await Userbot(client, FakeIntake()).start(asyncio.Event())

    assert client.calls == ["add_event_handler", "connect", "is_user_authorized", "catch_up"]


async def test_catch_up_is_not_called_for_an_unauthorized_session() -> None:
    client = FakeClient(authorized=False)

    with pytest.raises(SessionNotAuthorized):
        await Userbot(client, FakeIntake()).start(asyncio.Event())

    assert "catch_up" not in client.calls


async def test_delivered_messages_reach_the_intake_and_nothing_is_written() -> None:
    client = FakeClient()
    intake = FakeIntake()
    userbot = Userbot(client, intake)
    await userbot.start(asyncio.Event())

    await client.deliver(FakeEvent(id=11))
    await client.deliver(FakeEvent(id=12, out=True))

    assert [m.tg_message_id for m in intake.received] == [11]
    assert client.write_attempts == []


async def test_stop_removes_the_handler_before_disconnecting() -> None:
    client = FakeClient()
    userbot = Userbot(client, FakeIntake())
    await userbot.start(asyncio.Event())

    await userbot.stop()

    assert client.calls[-2:] == ["remove_event_handler", "disconnect"]
    assert client.handlers == []


async def test_run_until_returns_when_asked_to_stop() -> None:
    client = FakeClient()
    userbot = Userbot(client, FakeIntake())
    await userbot.start(asyncio.Event())
    stop = asyncio.Event()

    waiting = asyncio.create_task(userbot.run_until(stop))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    stop.set()

    await asyncio.wait_for(waiting, timeout=1)


async def test_unexpected_disconnect_is_an_error() -> None:
    client = FakeClient()
    userbot = Userbot(client, FakeIntake())
    await userbot.start(asyncio.Event())

    waiting = asyncio.create_task(userbot.run_until(asyncio.Event()))
    await asyncio.sleep(0.01)
    client.lose_connection()

    with pytest.raises(Disconnected):
        await asyncio.wait_for(waiting, timeout=1)


async def test_a_disconnect_future_that_failed_still_counts_as_a_disconnect() -> None:
    client = FakeClient()
    userbot = Userbot(client, FakeIntake())
    await userbot.start(asyncio.Event())
    client.disconnected.set_exception(ConnectionError("fake"))

    with pytest.raises(Disconnected):
        await asyncio.wait_for(userbot.run_until(asyncio.Event()), timeout=1)


async def test_stop_without_start_is_harmless() -> None:
    client = FakeClient()

    await Userbot(client, FakeIntake()).stop()

    assert client.calls == ["remove_event_handler", "disconnect"]


async def test_a_stop_signal_ends_a_connect_that_never_finishes() -> None:
    client = FakeClient(hang_on_connect=True)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)

    await asyncio.wait_for(Userbot(client, FakeIntake()).start(stop), timeout=1)

    assert client.calls == ["add_event_handler", "connect"]
    assert client.write_attempts == []


async def test_a_stop_signal_before_start_never_reaches_catch_up() -> None:
    client = FakeClient(hang_on_connect=True)
    stop = asyncio.Event()
    stop.set()

    await asyncio.wait_for(Userbot(client, FakeIntake()).start(stop), timeout=1)

    assert "catch_up" not in client.calls
    assert client.write_attempts == []


async def test_a_stop_signal_ends_a_catch_up_that_never_finishes() -> None:
    client = FakeClient(hang_on_catch_up=True)
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)

    await asyncio.wait_for(Userbot(client, FakeIntake()).start(stop), timeout=1)

    assert client.calls == ["add_event_handler", "connect", "is_user_authorized", "catch_up"]
    assert client.write_attempts == []


async def test_a_stopped_start_is_followed_by_a_clean_run_until_and_stop() -> None:
    client = FakeClient(hang_on_connect=True)
    userbot = Userbot(client, FakeIntake())
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)

    await asyncio.wait_for(userbot.start(stop), timeout=1)
    await asyncio.wait_for(userbot.run_until(stop), timeout=1)
    await asyncio.wait_for(userbot.stop(), timeout=1)

    assert client.calls[-2:] == ["remove_event_handler", "disconnect"]
    assert client.write_attempts == []


async def test_the_connect_timeout_still_applies_when_no_stop_signal_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "CONNECT_TIMEOUT_SECONDS", 0.05)
    client = FakeClient(hang_on_connect=True)

    with pytest.raises(ConnectTimedOut):
        await asyncio.wait_for(Userbot(client, FakeIntake()).start(asyncio.Event()), timeout=2)

    assert client.calls == ["add_event_handler", "connect", "disconnect"]
    assert client.write_attempts == []


async def test_a_failure_during_start_is_not_hidden_by_the_race() -> None:
    client = FakeClient(authorized=False)

    with pytest.raises(SessionNotAuthorized):
        await Userbot(client, FakeIntake()).start(asyncio.Event())

    assert client.write_attempts == []
