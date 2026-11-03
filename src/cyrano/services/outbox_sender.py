"""The one sender: claims due outbox rows and sends them, one at a time, behind the guard."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cyrano.domain.errors import (
    OutboxStateConflict,
    SendFailure,
    SendFloodWait,
    SendRejected,
)
from cyrano.domain.outbox import (
    CAUSE_SEND_DEADLINE,
    ERROR_INTERRUPTED,
    ERROR_UNCERTAIN,
    ScheduledMessage,
)
from cyrano.services.message_transport import MessageTransport
from cyrano.services.owner_notifier import OwnerNotifier
from cyrano.services.repositories import ContactRepository, OutboxRepository
from cyrano.services.send_guard import SendGuard

logger = logging.getLogger("cyrano.services.outbox_sender")

SENDER_POLL_INTERVAL = 2.0
SENDER_STALE_CHECK_INTERVAL = 60.0
SENDING_STALE_AFTER = 120.0
SENDER_SEND_DEADLINE = 100.0
FLOOD_WAIT_MARGIN = 5.0
FLOOD_WAIT_NOTIFY_ABOVE = 60.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SenderTimings:
    poll_interval: float = SENDER_POLL_INTERVAL
    stale_check_interval: float = SENDER_STALE_CHECK_INTERVAL
    stale_after: float = SENDING_STALE_AFTER
    send_deadline: float = SENDER_SEND_DEADLINE
    flood_margin: float = FLOOD_WAIT_MARGIN
    flood_notify_above: float = FLOOD_WAIT_NOTIFY_ABOVE


@dataclass(frozen=True, slots=True)
class SenderNotices:
    interrupted: str
    failed: str
    uncertain: str
    flood_wait: str


class OutboxSender:
    def __init__(
        self,
        outbox: OutboxRepository,
        contacts: ContactRepository,
        transport: MessageTransport,
        notifier: OwnerNotifier,
        guard: SendGuard,
        notices: SenderNotices,
        *,
        clock: Callable[[], datetime] = _utc_now,
        timings: SenderTimings | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._outbox = outbox
        self._contacts = contacts
        self._transport = transport
        self._notifier = notifier
        self._guard = guard
        self._notices = notices
        self._clock = clock
        self._timings = timings if timings is not None else SenderTimings()
        self._sleep = sleep

    async def run(self) -> None:
        await self._step(self.sweep_interrupted)
        await self.poll_forever()

    async def poll_forever(self) -> None:
        stale_every = timedelta(seconds=self._timings.stale_check_interval)
        next_stale_check = self._clock() + stale_every
        while True:
            if self._clock() >= next_stale_check:
                next_stale_check = self._clock() + stale_every
                await self._step(self.fail_stale)
            await self._step(self.send_next)
            await self._sleep(self._timings.poll_interval)

    async def sweep_interrupted(self) -> None:
        await self._fail_interrupted(self._clock())

    async def fail_stale(self) -> None:
        await self._fail_interrupted(self._clock() - timedelta(seconds=self._timings.stale_after))

    async def send_next(self) -> bool:
        row = await self._outbox.claim_due(self._clock())
        if row is None:
            return False
        await self._deliver(row)
        return True

    async def _step(self, step: Callable[[], Awaitable[object]]) -> None:
        try:
            await step()
        except Exception as exc:
            logger.error("outbox sender step failed", extra={"error": type(exc).__name__})

    async def _fail_interrupted(self, claimed_before: datetime) -> None:
        for row in await self._outbox.stale_sending(claimed_before):
            try:
                await self._outbox.mark_failed(row.id, ERROR_INTERRUPTED)
            except OutboxStateConflict:
                continue
            logger.warning(
                "outbox row interrupted", extra={"row_id": row.id, "contact_id": row.contact_id}
            )
            await self._notify(
                self._notices.interrupted.format(
                    row_id=row.id, contact_id=row.contact_id, error=ERROR_INTERRUPTED
                )
            )

    async def _deliver(self, row: ScheduledMessage) -> None:
        contact = await self._contacts.get_by_id(row.contact_id)
        decision = self._guard.check(contact, row.source)
        if contact is None or not decision.allowed:
            await self._outbox.mark_cancelled(row.id, decision.reason)
            logger.info(
                "outbox row refused by the guard",
                extra={"row_id": row.id, "contact_id": row.contact_id, "reason": decision.reason},
            )
            return
        deadline = asyncio.timeout(self._timings.send_deadline)
        try:
            async with deadline:
                sent = await self._transport.send_text(contact.tg_id, row.text)
        except SendFailure as exc:
            await self._handle_failure(row, exc)
            return
        except Exception as exc:
            cause = CAUSE_SEND_DEADLINE if deadline.expired() else type(exc).__name__
            await self._fail_uncertain(row, cause)
            return
        await self._outbox.complete_send(
            row.id, row.contact_id, row.text, sent.tg_message_id, sent.sent_at
        )
        logger.info("outbox row sent", extra={"row_id": row.id, "contact_id": row.contact_id})

    async def _handle_failure(self, row: ScheduledMessage, failure: SendFailure) -> None:
        if isinstance(failure, SendFloodWait):
            await self._handle_flood_wait(row, failure)
        elif isinstance(failure, SendRejected):
            await self._fail_rejected(row, failure.error_class)
        else:
            await self._fail_uncertain(row, failure.error_class)

    async def _handle_flood_wait(self, row: ScheduledMessage, failure: SendFloodWait) -> None:
        delay = failure.seconds + self._timings.flood_margin
        await self._outbox.reschedule(
            row.id, self._clock() + timedelta(seconds=delay), failure.error_class
        )
        logger.warning(
            "outbox row delayed by a flood wait",
            extra={"row_id": row.id, "contact_id": row.contact_id, "seconds": failure.seconds},
        )
        if failure.seconds > self._timings.flood_notify_above:
            await self._notify(
                self._notices.flood_wait.format(
                    row_id=row.id, contact_id=row.contact_id, seconds=failure.seconds
                )
            )

    async def _fail_rejected(self, row: ScheduledMessage, error_class: str) -> None:
        await self._outbox.mark_failed(row.id, error_class)
        logger.warning(
            "outbox row failed",
            extra={"row_id": row.id, "contact_id": row.contact_id, "error": error_class},
        )
        await self._notify(
            self._notices.failed.format(row_id=row.id, contact_id=row.contact_id, error=error_class)
        )

    async def _fail_uncertain(self, row: ScheduledMessage, cause: str) -> None:
        await self._outbox.mark_failed(row.id, ERROR_UNCERTAIN)
        logger.warning(
            "outbox row outcome is unknown",
            extra={"row_id": row.id, "contact_id": row.contact_id, "error": cause},
        )
        await self._notify(self._notices.uncertain.format(row_id=row.id, contact_id=row.contact_id))

    async def _notify(self, text: str) -> None:
        try:
            await self._notifier.notify(text)
        except Exception as exc:
            logger.warning("owner notification failed", extra={"error": type(exc).__name__})
