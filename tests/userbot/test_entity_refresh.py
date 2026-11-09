from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest
from telethon.errors import FloodWaitError, PeerFloodError

from cyrano.domain.enums import RejectionReason
from cyrano.domain.errors import SendFloodWait, SendRejected
from cyrano.logging import JsonFormatter
from cyrano.userbot import adapter as adapter_module
from cyrano.userbot.adapter import (
    ENTITY_REFRESH_LIMIT,
    ENTITY_REFRESH_MIN_INTERVAL_SECONDS,
    PEER_NOT_FOUND,
    TelethonAdapter,
)
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.services.outbox_fakes import FakeClock

OTHER_TELEGRAM_ID = FAKE_TELEGRAM_ID + 1
TEXT = "refresh-text-marker-zq"
ERROR_DETAIL = "refresh-detail-marker-zq-name"
SEEN_DIALOGS = 7
PEER = object()
ADAPTER_LOGGER = "cyrano.userbot"


class _Raw:
    def __init__(
        self,
        *,
        cached: frozenset[int] = frozenset(),
        reveals: frozenset[int] = frozenset({FAKE_TELEGRAM_ID}),
        dialogs: int = SEEN_DIALOGS,
        refresh_error: Exception | None = None,
        lookup_failures: int = 0,
        lookup_error: Exception | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.known = set(cached)
        self.lookups: list[int] = []
        self.refresh_limits: list[int] = []
        self.refresh_started = asyncio.Event()
        self.sent: list[object] = []
        self.typed: list[object] = []
        self.read: list[object] = []
        self._reveals = reveals
        self._dialogs = dialogs
        self._refresh_error = refresh_error
        self.lookup_failures = lookup_failures
        self._lookup_error = lookup_error
        self.gate = gate

    async def get_input_entity(self, peer: int) -> object:
        self.lookups.append(peer)
        if self._lookup_error is not None:
            raise self._lookup_error
        if self.lookup_failures > 0:
            self.lookup_failures -= 1
            raise ValueError("not in the cache")
        if peer not in self.known:
            raise ValueError("not in the cache")
        return PEER

    async def iter_dialogs(self, limit: int) -> AsyncIterator[object]:
        self.refresh_limits.append(limit)
        self.refresh_started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self._refresh_error is not None:
            raise self._refresh_error
        self.known |= self._reveals
        for index in range(self._dialogs):
            yield index

    async def send_message(self, peer: object, text: str, **kwargs: object) -> object:
        self.sent.append(peer)
        return _Sent()

    def action(self, peer: object, action: str, **kwargs: object) -> _Typing:
        self.typed.append(peer)
        return _Typing()

    async def send_read_acknowledge(self, peer: object) -> bool:
        self.read.append(peer)
        return True


class _Sent:
    id = 11
    date = FakeClock().now


class _Typing:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


async def _no_sleep(_: float) -> None:
    return None


def _adapter(raw: _Raw, clock: FakeClock | None = None) -> TelethonAdapter:
    return TelethonAdapter(raw, sleep=_no_sleep, clock=clock if clock is not None else FakeClock())


async def _call(adapter: TelethonAdapter, method: str, tg_id: int = FAKE_TELEGRAM_ID) -> None:
    if method == "send":
        await adapter.send_text(tg_id, TEXT)
    elif method == "typing":
        await adapter.show_typing(tg_id, 1)
    else:
        await adapter.mark_read(tg_id)


def _done(raw: _Raw, method: str) -> list[object]:
    return {"send": raw.sent, "typing": raw.typed, "read": raw.read}[method]


METHODS = ["send", "typing", "read"]


@pytest.mark.parametrize("method", METHODS)
async def test_a_known_peer_is_resolved_directly_without_a_refresh(method: str) -> None:
    raw = _Raw(cached=frozenset({FAKE_TELEGRAM_ID}))

    await _call(_adapter(raw), method)

    assert raw.lookups == [FAKE_TELEGRAM_ID]
    assert raw.refresh_limits == []
    assert _done(raw, method) == [PEER]


@pytest.mark.parametrize("method", METHODS)
async def test_an_unknown_peer_is_found_after_one_refresh_and_a_second_attempt(
    method: str,
) -> None:
    raw = _Raw()

    await _call(_adapter(raw), method)

    assert raw.lookups == [FAKE_TELEGRAM_ID, FAKE_TELEGRAM_ID]
    assert raw.refresh_limits == [ENTITY_REFRESH_LIMIT]
    assert _done(raw, method) == [PEER]


@pytest.mark.parametrize("method", METHODS)
async def test_a_peer_still_unknown_after_the_refresh_is_rejected_as_before(method: str) -> None:
    raw = _Raw(reveals=frozenset())

    with pytest.raises(SendRejected) as caught:
        await _call(_adapter(raw), method)

    assert caught.value.error_class == PEER_NOT_FOUND
    assert caught.value.reason is RejectionReason.RECIPIENT
    assert raw.lookups == [FAKE_TELEGRAM_ID, FAKE_TELEGRAM_ID]
    assert raw.refresh_limits == [ENTITY_REFRESH_LIMIT]
    assert (raw.sent, raw.typed, raw.read) == ([], [], [])


async def test_the_refresh_is_not_called_for_a_failure_that_is_not_a_missing_peer() -> None:
    raw = _Raw(lookup_error=PeerFloodError(request=None))

    with pytest.raises(Exception) as caught:
        await _adapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert type(caught.value).__name__ == "SendAccountBlocked"
    assert raw.lookups == [FAKE_TELEGRAM_ID]
    assert raw.refresh_limits == []


async def test_a_flood_wait_on_the_second_attempt_is_a_flood_wait_not_a_missing_peer() -> None:
    class _Flaky(_Raw):
        async def get_input_entity(self, peer: int) -> object:
            self.lookups.append(peer)
            if len(self.lookups) == 1:
                raise ValueError("not in the cache")
            raise FloodWaitError(request=None, capture=9)

    raw = _Flaky()

    with pytest.raises(SendFloodWait) as caught:
        await _adapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert caught.value.seconds == 9
    assert raw.refresh_limits == [ENTITY_REFRESH_LIMIT]


async def test_the_public_refresh_reads_the_dialogs_completely_and_counts_them() -> None:
    raw = _Raw(dialogs=SEEN_DIALOGS)

    seen = await _adapter(raw).refresh_entities(500)

    assert seen == SEEN_DIALOGS
    assert raw.refresh_limits == [500]
    assert raw.sent == []
    assert raw.typed == []
    assert raw.read == []


async def test_a_second_failure_inside_the_interval_does_not_refresh_again() -> None:
    clock = FakeClock()
    raw = _Raw(reveals=frozenset())
    adapter = _adapter(raw, clock)

    with pytest.raises(SendRejected):
        await adapter.send_text(FAKE_TELEGRAM_ID, TEXT)
    clock.advance(ENTITY_REFRESH_MIN_INTERVAL_SECONDS - 1)
    with pytest.raises(SendRejected):
        await adapter.send_text(OTHER_TELEGRAM_ID, TEXT)

    assert raw.refresh_limits == [ENTITY_REFRESH_LIMIT]
    assert raw.lookups == [FAKE_TELEGRAM_ID, FAKE_TELEGRAM_ID, OTHER_TELEGRAM_ID, OTHER_TELEGRAM_ID]


async def test_a_skipped_refresh_still_gets_the_second_attempt_which_may_succeed() -> None:
    clock = FakeClock()
    raw = _Raw(reveals=frozenset({FAKE_TELEGRAM_ID, OTHER_TELEGRAM_ID}))
    adapter = _adapter(raw, clock)
    await adapter.send_text(FAKE_TELEGRAM_ID, TEXT)
    raw.lookup_failures = 1

    await adapter.send_text(OTHER_TELEGRAM_ID, TEXT)

    assert raw.refresh_limits == [ENTITY_REFRESH_LIMIT]
    assert raw.sent == [PEER, PEER]
    assert raw.lookups == [FAKE_TELEGRAM_ID, FAKE_TELEGRAM_ID, OTHER_TELEGRAM_ID, OTHER_TELEGRAM_ID]


async def test_the_refresh_is_allowed_again_when_the_interval_has_passed() -> None:
    clock = FakeClock()
    raw = _Raw(reveals=frozenset())
    adapter = _adapter(raw, clock)

    with pytest.raises(SendRejected):
        await adapter.send_text(FAKE_TELEGRAM_ID, TEXT)
    clock.advance(ENTITY_REFRESH_MIN_INTERVAL_SECONDS)
    with pytest.raises(SendRejected):
        await adapter.send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raw.refresh_limits == [ENTITY_REFRESH_LIMIT, ENTITY_REFRESH_LIMIT]


async def test_the_interval_counts_from_the_end_of_a_slow_refresh() -> None:
    clock = FakeClock()

    class _Slow(_Raw):
        async def iter_dialogs(self, limit: int) -> AsyncIterator[object]:
            clock.advance(ENTITY_REFRESH_MIN_INTERVAL_SECONDS - 1)
            async for item in super().iter_dialogs(limit):
                yield item

    raw = _Slow(reveals=frozenset())
    adapter = _adapter(raw, clock)

    with pytest.raises(SendRejected):
        await adapter.send_text(FAKE_TELEGRAM_ID, TEXT)
    clock.advance(ENTITY_REFRESH_MIN_INTERVAL_SECONDS - 5)
    with pytest.raises(SendRejected):
        await adapter.send_text(FAKE_TELEGRAM_ID, TEXT)

    assert len(raw.refresh_limits) == 1


async def test_the_warm_up_call_and_a_failing_send_share_one_rate_limit() -> None:
    raw = _Raw(reveals=frozenset())
    adapter = _adapter(raw)

    assert await adapter.refresh_entities(500) == SEEN_DIALOGS
    with pytest.raises(SendRejected):
        await adapter.send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raw.refresh_limits == [500]


async def test_a_refresh_that_is_skipped_reports_no_dialogs() -> None:
    raw = _Raw()
    adapter = _adapter(raw)
    await adapter.refresh_entities(500)

    assert await adapter.refresh_entities(500) == 0
    assert raw.refresh_limits == [500]


@pytest.mark.parametrize("method", METHODS)
async def test_two_concurrent_failing_calls_wait_for_one_refresh(method: str) -> None:
    gate = asyncio.Event()
    raw = _Raw(gate=gate)
    adapter = _adapter(raw)

    first = asyncio.create_task(_call(adapter, method))
    await raw.refresh_started.wait()
    second = asyncio.create_task(_call(adapter, method, FAKE_TELEGRAM_ID))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not second.done()
    gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    assert raw.refresh_limits == [ENTITY_REFRESH_LIMIT]
    assert len(_done(raw, method)) == 2


REFRESH_FAILURES = [
    ConnectionError(ERROR_DETAIL),
    OSError(ERROR_DETAIL),
    RuntimeError(ERROR_DETAIL),
    FloodWaitError(request=None, capture=45),
    PeerFloodError(request=None),
    TimeoutError(),
]


@pytest.mark.parametrize("error", REFRESH_FAILURES, ids=lambda e: type(e).__name__)
async def test_a_failing_refresh_never_becomes_a_send_failure_when_the_second_attempt_works(
    error: Exception,
) -> None:
    raw = _Raw(refresh_error=error, lookup_failures=1, cached=frozenset({FAKE_TELEGRAM_ID}))

    sent = await _adapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert sent.tg_message_id == _Sent.id
    assert raw.lookups == [FAKE_TELEGRAM_ID, FAKE_TELEGRAM_ID]
    assert raw.refresh_limits == [ENTITY_REFRESH_LIMIT]


@pytest.mark.parametrize("error", REFRESH_FAILURES, ids=lambda e: type(e).__name__)
async def test_a_failing_refresh_leaves_only_the_missing_peer_rejection_when_still_unknown(
    error: Exception,
) -> None:
    raw = _Raw(refresh_error=error)

    with pytest.raises(SendRejected) as caught:
        await _adapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    assert caught.value.error_class == PEER_NOT_FOUND
    assert raw.lookups == [FAKE_TELEGRAM_ID, FAKE_TELEGRAM_ID]


async def test_a_failed_refresh_still_counts_for_the_rate_limit() -> None:
    raw = _Raw(refresh_error=FloodWaitError(request=None, capture=45))
    adapter = _adapter(raw)

    for _ in range(3):
        with pytest.raises(SendRejected):
            await adapter.send_text(FAKE_TELEGRAM_ID, TEXT)

    assert raw.refresh_limits == [ENTITY_REFRESH_LIMIT]


async def test_a_refresh_that_runs_past_its_deadline_is_a_failed_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter_module, "ENTITY_REFRESH_DEADLINE_SECONDS", 0.01)
    raw = _Raw(gate=asyncio.Event())

    with pytest.raises(SendRejected) as caught:
        await asyncio.wait_for(_adapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT), timeout=2)

    assert caught.value.error_class == PEER_NOT_FOUND
    assert raw.refresh_limits == [ENTITY_REFRESH_LIMIT]


async def test_cancelling_a_waiting_refresh_is_not_swallowed_and_frees_the_lock() -> None:
    clock = FakeClock()
    raw = _Raw(gate=asyncio.Event())
    adapter = _adapter(raw, clock)
    task = asyncio.create_task(adapter.send_text(FAKE_TELEGRAM_ID, TEXT))
    await raw.refresh_started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    raw.gate = None
    clock.advance(ENTITY_REFRESH_MIN_INTERVAL_SECONDS)
    assert await asyncio.wait_for(adapter.refresh_entities(500), timeout=1) == SEEN_DIALOGS


def _json_lines(caplog: pytest.LogCaptureFixture) -> str:
    formatter = JsonFormatter()
    return "\n".join(formatter.format(record) for record in caplog.records)


async def test_a_refresh_logs_counters_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    clock = FakeClock()
    raw = _Raw()

    await _adapter(raw, clock).send_text(FAKE_TELEGRAM_ID, TEXT)

    record = next(r for r in caplog.records if r.name == ADAPTER_LOGGER)
    assert record.getMessage() == "entity refresh finished"
    assert (record.__dict__["refreshed"], record.__dict__["dialogs"]) == (True, SEEN_DIALOGS)
    assert record.__dict__["duration_seconds"] == 0


async def test_a_failed_refresh_logs_the_class_name_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    raw = _Raw(refresh_error=RuntimeError(ERROR_DETAIL), reveals=frozenset())

    with pytest.raises(SendRejected):
        await _adapter(raw).send_text(FAKE_TELEGRAM_ID, TEXT)

    record = next(r for r in caplog.records if r.getMessage() == "entity refresh failed")
    assert record.__dict__["error"] == "RuntimeError"
    assert record.__dict__["failed_refreshes"] == 1
    assert record.levelno == logging.WARNING


async def test_a_skipped_refresh_is_logged_as_not_refreshed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = _adapter(_Raw())
    await adapter.refresh_entities(500)
    caplog.clear()

    await adapter.refresh_entities(500)

    assert [(r.getMessage(), r.__dict__["refreshed"]) for r in caplog.records] == [
        ("entity refresh skipped", False)
    ]


@pytest.mark.parametrize("failing", [False, True])
async def test_the_logs_carry_no_id_text_or_error_detail(
    caplog: pytest.LogCaptureFixture, failing: bool
) -> None:
    caplog.set_level(logging.DEBUG)
    raw = _Raw(refresh_error=RuntimeError(ERROR_DETAIL) if failing else None)
    adapter = _adapter(raw)

    if failing:
        with pytest.raises(SendRejected):
            await adapter.send_text(FAKE_TELEGRAM_ID, TEXT)
    else:
        await adapter.send_text(FAKE_TELEGRAM_ID, TEXT)

    haystack = _json_lines(caplog)
    assert haystack
    for marker in (FAKE_TELEGRAM_ID, OTHER_TELEGRAM_ID, TEXT, ERROR_DETAIL):
        assert_absent(marker, haystack)
