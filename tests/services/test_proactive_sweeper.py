from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

import pytest

from cyrano.domain.autopilot import SWEEP_INTERVAL_SECONDS
from cyrano.domain.enums import AutopilotStopReason, ContactStatus, MessageDirection
from cyrano.domain.proactive import (
    PROACTIVE_SWEEP_LIMIT,
    DialogueTail,
    ProactiveDue,
    proactive_job_id,
)
from cyrano.services.proactive_job_queue import ProactiveJobQueue
from cyrano.services.proactive_sweeper import ProactiveSweeper
from cyrano.services.repositories import ProactiveRepository
from tests.capped_loop import CappedLoop
from tests.services.autopilot_rig import AutopilotRig, json_logs
from tests.services.proactive_rig import OUR_TEXT_ZQ, ProactiveRig


@dataclass
class _Queue:
    errors: list[Exception] = field(default_factory=list)
    jobs: list[tuple[int, int]] = field(default_factory=list)

    async def enqueue(self, contact_id: int, last_message_id: int) -> None:
        if self.errors:
            raise self.errors.pop(0)
        self.jobs.append((contact_id, last_message_id))


class _FlakyScan:
    def __init__(self, inner: ProactiveRepository, failures: int) -> None:
        self._inner = inner
        self.failures = failures
        self.scans = 0

    async def dialogue_tail(self, contact_id: int) -> DialogueTail:
        return await self._inner.dialogue_tail(contact_id)

    async def count_proactive_after(self, contact_id: int, message_id: int) -> int:
        return await self._inner.count_proactive_after(contact_id, message_id)

    async def newest_proactive_due_at(self, contact_id: int) -> datetime | None:
        return await self._inner.newest_proactive_due_at(contact_id)

    async def count_proactive_due_since(self, since: datetime) -> int:
        return await self._inner.count_proactive_due_since(since)

    async def contacts_due_for_proactive(
        self,
        *,
        silent_since: datetime,
        silent_not_before: datetime,
        now: datetime,
        max_per_silence: int,
        interval_since: datetime,
        limit: int,
    ) -> list[ProactiveDue]:
        self.scans += 1
        if self.failures:
            self.failures -= 1
            raise RuntimeError("invented storage failure")
        return await self._inner.contacts_due_for_proactive(
            silent_since=silent_since,
            silent_not_before=silent_not_before,
            now=now,
            max_per_silence=max_per_silence,
            interval_since=interval_since,
            limit=limit,
        )

    async def set_proactive_not_before(self, contact_id: int, when: datetime) -> None:
        await self._inner.set_proactive_not_before(contact_id, when)


def _sweeper(
    prig: ProactiveRig,
    queue: _Queue,
    *,
    scan: ProactiveRepository | None = None,
    limit: int = PROACTIVE_SWEEP_LIMIT,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ProactiveSweeper:
    return ProactiveSweeper(
        scan if scan is not None else prig.proactive,
        prig.eligibility(),
        queue,
        clock=prig.rig.clock,
        sleep=sleep,
        limit=limit,
    )


def test_the_fakes_satisfy_their_protocols(prig: ProactiveRig) -> None:
    queue: ProactiveJobQueue = _Queue()
    scan: ProactiveRepository = _FlakyScan(prig.proactive, 0)

    assert queue is not None
    assert scan is not None


def test_the_job_id_is_deterministic_and_the_limit_is_ten() -> None:
    assert proactive_job_id(7, 41) == "proactive-7-41"
    assert PROACTIVE_SWEEP_LIMIT == 10


async def test_a_scan_enqueues_one_job_per_due_contact_with_its_last_message(
    prig: ProactiveRig,
) -> None:
    due_one, last_one = await prig.silent_contact(7)
    due_two, last_two = await prig.silent_contact(9)
    short, _ = await prig.silent_contact(2)
    answered = await prig.rig.contact()
    await prig.rig.say(answered, ago=9 * 3600, direction=MessageDirection.OUTGOING)
    await prig.rig.say(answered, ago=8 * 3600)
    paused, _ = await prig.silent_contact(8)
    await prig.rig.contacts.set_status(paused.id, ContactStatus.PAUSED)
    queue = _Queue()

    enqueued = await _sweeper(prig, queue).sweep()

    assert enqueued == 2
    assert queue.jobs == [(due_one.id, last_one.id), (due_two.id, last_two.id)]
    assert {short.id, answered.id, paused.id}.isdisjoint(job[0] for job in queue.jobs)


async def test_a_long_silent_contact_is_not_enqueued_while_a_recent_one_is(
    prig: ProactiveRig,
) -> None:
    long_silent, _ = await prig.silent_contact(100)
    recent, last = await prig.silent_contact(7)
    queue = _Queue()

    assert await _sweeper(prig, queue).sweep() == 1

    assert queue.jobs == [(recent.id, last.id)]
    assert (await prig.rig.reload(long_silent)).proactive_not_before is None
    assert await prig.rig.outbox.list_rows(None, 10) == []


async def test_the_silence_exactly_at_the_upper_bound_is_enqueued_and_one_second_more_is_not(
    prig: ProactiveRig,
) -> None:
    at_limit, last = await prig.silent_contact(72)
    over, _ = await prig.silent_contact(72 + 1 / 3600)
    queue = _Queue()

    await _sweeper(prig, queue).sweep()

    assert queue.jobs == [(at_limit.id, last.id)]
    assert over.id not in {job[0] for job in queue.jobs}


async def test_the_upper_bound_follows_the_setting(rig: AutopilotRig) -> None:
    prig = ProactiveRig(rig, replace(ProactiveRig(rig).rules, proactive_max_silence_days=1))
    await prig.silent_contact(30)
    inside, last = await prig.silent_contact(20)
    queue = _Queue()

    await _sweeper(prig, queue).sweep()

    assert queue.jobs == [(inside.id, last.id)]


async def test_the_scan_keeps_to_its_limit(prig: ProactiveRig) -> None:
    for hours in range(7, 12):
        await prig.silent_contact(hours)
    queue = _Queue()

    assert await _sweeper(prig, queue, limit=3).sweep() == 3
    assert len(queue.jobs) == 3


@pytest.mark.parametrize("switch", ["proactive_enabled", "autopilot_enabled"])
async def test_the_scan_does_nothing_with_either_switch_off(rig: AutopilotRig, switch: str) -> None:
    prig = ProactiveRig(rig)
    prig.rules = (
        replace(prig.rules, proactive_enabled=False)
        if switch == "proactive_enabled"
        else replace(prig.rules, autopilot_enabled=False)
    )
    await prig.silent_contact()
    scan = _FlakyScan(prig.proactive, 0)
    queue = _Queue()

    assert await _sweeper(prig, queue, scan=scan).sweep() == 0
    assert queue.jobs == []
    assert scan.scans == 0


async def test_the_pure_rule_filters_what_the_storage_found(prig: ProactiveRig) -> None:
    await prig.silent_contact()
    await prig.rig.state.stop(AutopilotStopReason.PROVIDER_AUTH)
    queue = _Queue()

    assert await _sweeper(prig, queue).sweep() == 0
    assert queue.jobs == []


async def test_a_queue_failure_is_logged_by_class_and_the_scan_goes_on(
    prig: ProactiveRig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    await prig.silent_contact(7)
    _, last = await prig.silent_contact(8)
    queue = _Queue(errors=[ConnectionError("invented redis failure text-zq")])

    enqueued = await _sweeper(prig, queue).sweep()

    assert enqueued == 1
    assert queue.jobs[0][1] == last.id
    [line] = [
        r for r in caplog.records if r.getMessage() == "proactive sweep could not enqueue every job"
    ]
    assert line.__dict__["error"] == "ConnectionError"
    assert (line.__dict__["found"], line.__dict__["eligible"], line.__dict__["enqueued"]) == (
        2,
        2,
        1,
    )
    output = json_logs(caplog)
    assert "text-zq" not in output
    assert OUR_TEXT_ZQ not in output


async def test_the_loop_sweeps_every_interval_and_survives_a_failing_sweep(
    prig: ProactiveRig, caplog: pytest.LogCaptureFixture
) -> None:
    contact, last = await prig.silent_contact()
    queue = _Queue()
    sleeps: list[float] = []

    async def on_sleep(seconds: float) -> bool:
        sleeps.append(seconds)
        return len(queue.jobs) >= 2

    capped = CappedLoop(on_sleep, cap=10)
    sweeper = _sweeper(prig, queue, scan=_FlakyScan(prig.proactive, 1), sleep=capped.sleep)

    await capped.run(sweeper.run())

    assert sleeps == [SWEEP_INTERVAL_SECONDS] * 3
    assert queue.jobs == [(contact.id, last.id)] * 2
    errors = [r for r in caplog.records if r.getMessage() == "proactive sweep failed"]
    assert [r.__dict__["error"] for r in errors] == ["RuntimeError"]
