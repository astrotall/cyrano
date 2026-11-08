from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cyrano.domain.enums import ProactiveJobOutcome, ProactiveJobReason, ProactiveReason
from cyrano.services.proactive_sweeper import ProactiveSweeper
from tests.services.autopilot_rig import SequenceLLMClient, llm_result
from tests.services.proactive_rig import FOLLOW_UP_TEXT_ZQ, ProactiveRig


class _Queue:
    def __init__(self) -> None:
        self.jobs: list[tuple[int, int]] = []

    async def enqueue(self, contact_id: int, last_message_id: int) -> None:
        self.jobs.append((contact_id, last_message_id))


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 11, 4, 23, 0, tzinfo=UTC),
        datetime(2026, 11, 4, 23, 30, tzinfo=UTC),
        datetime(2026, 11, 4, 23, 50, tzinfo=UTC),
        datetime(2026, 11, 4, 23, 59, 59, tzinfo=UTC),
    ],
)
async def test_no_proactive_message_starts_while_the_owner_is_about_to_leave(
    prig: ProactiveRig, moment: datetime
) -> None:
    prig.rig.clock.now = moment
    contact, last = await prig.silent_contact()
    client = SequenceLLMClient(llm_result(FOLLOW_UP_TEXT_ZQ))

    check = await prig.eligibility().check(contact.id)
    result = await prig.runner(client).run(contact.id, last.id)

    assert check.reason is ProactiveReason.UNAVAILABLE_SOON
    assert check.snapshot.unavailable is not None
    assert result.outcome is ProactiveJobOutcome.SKIPPED
    assert result.reason is ProactiveJobReason.UNAVAILABLE_SOON
    assert client.calls == 0
    assert await prig.rig.outbox.list_rows(None, 10) == []


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 11, 4, 22, 59, 59, tzinfo=UTC),
        datetime(2026, 11, 4, 15, 0, tzinfo=UTC),
    ],
)
async def test_outside_the_wrapup_time_the_rule_does_not_block(
    prig: ProactiveRig, moment: datetime
) -> None:
    prig.rig.clock.now = moment
    contact, _ = await prig.silent_contact()

    check = await prig.eligibility().check(contact.id)

    assert check.reason is ProactiveReason.ELIGIBLE
    assert check.snapshot.unavailable is None


async def test_the_sweeper_enqueues_nothing_while_the_owner_is_about_to_leave(
    prig: ProactiveRig,
) -> None:
    prig.rig.clock.now = datetime(2026, 11, 4, 23, 45, tzinfo=UTC)
    await prig.silent_contact()
    queue = _Queue()

    sweeper = ProactiveSweeper(prig.proactive, prig.eligibility(), queue, clock=prig.rig.clock)

    assert await sweeper.sweep() == 0
    assert queue.jobs == []
