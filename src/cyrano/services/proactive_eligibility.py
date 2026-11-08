from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from cyrano.domain.enums import ContactStatus, MessageDirection, ProactiveReason
from cyrano.domain.proactive import (
    HOURS_PER_DAY,
    PROACTIVE_DAILY_WINDOW_SECONDS,
    SECONDS_PER_HOUR,
    ProactiveSnapshot,
)
from cyrano.services.humanization import ScheduleService
from cyrano.services.repositories import (
    AutopilotStateRepository,
    ContactRepository,
    ProactiveRepository,
    SenderStateRepository,
)


class ProactiveSettings(Protocol):
    @property
    def proactive_enabled(self) -> bool: ...

    @property
    def autopilot_enabled(self) -> bool: ...

    @property
    def proactive_after_hours(self) -> int: ...

    @property
    def proactive_max_silence_days(self) -> int: ...

    @property
    def proactive_max_per_silence(self) -> int: ...

    @property
    def proactive_min_interval_hours(self) -> int: ...

    @property
    def proactive_daily_limit(self) -> int: ...


def hours(value: int) -> timedelta:
    return timedelta(seconds=value * SECONDS_PER_HOUR)


def max_silence(rules: ProactiveSettings) -> timedelta:
    return hours(rules.proactive_max_silence_days * HOURS_PER_DAY)


def switch_reason(rules: ProactiveSettings) -> ProactiveReason | None:
    if not rules.proactive_enabled:
        return ProactiveReason.PROACTIVE_DISABLED
    if not rules.autopilot_enabled:
        return ProactiveReason.AUTOPILOT_DISABLED
    return None


def silence_started_at(snapshot: ProactiveSnapshot) -> datetime | None:
    last = snapshot.tail.last
    if last is None or snapshot.contact is None:
        return None
    reply_after = snapshot.contact.reply_after
    return last.created_at if reply_after is None else max(last.created_at, reply_after)


def decide_proactive(snapshot: ProactiveSnapshot, rules: ProactiveSettings) -> ProactiveReason:
    switched_off = switch_reason(rules)
    if switched_off is not None:
        return switched_off
    now = snapshot.now
    if snapshot.autopilot.stopped:
        return ProactiveReason.AUTOPILOT_STOPPED
    if snapshot.autopilot.is_paused(now):
        return ProactiveReason.AUTOPILOT_PAUSED
    if snapshot.sender.stopped:
        return ProactiveReason.SENDER_STOPPED
    if snapshot.sender.is_paused(now):
        return ProactiveReason.SENDER_PAUSED
    if snapshot.unavailable is not None:
        return ProactiveReason.UNAVAILABLE_SOON
    contact = snapshot.contact
    if contact is None:
        return ProactiveReason.CONTACT_MISSING
    if contact.status is not ContactStatus.ACTIVE:
        return ProactiveReason.CONTACT_NOT_ACTIVE
    if contact.proactive_not_before is not None and contact.proactive_not_before > now:
        return ProactiveReason.POSTPONED
    last = snapshot.tail.last
    if last is None or snapshot.tail.last_incoming is None:
        return ProactiveReason.NO_INCOMING
    if last.direction is MessageDirection.INCOMING:
        return ProactiveReason.LAST_MESSAGE_INCOMING
    started = silence_started_at(snapshot)
    if started is None or now - started < hours(rules.proactive_after_hours):
        return ProactiveReason.SILENCE_TOO_SHORT
    if now - started > max_silence(rules):
        return ProactiveReason.SILENCE_TOO_LONG
    if snapshot.silence_rows >= rules.proactive_max_per_silence:
        return ProactiveReason.SILENCE_LIMIT_REACHED
    newest = snapshot.newest_row_due_at
    if newest is not None and now - newest < hours(rules.proactive_min_interval_hours):
        return ProactiveReason.MIN_INTERVAL
    if snapshot.daily_rows >= rules.proactive_daily_limit:
        return ProactiveReason.DAILY_LIMIT
    return ProactiveReason.ELIGIBLE


@dataclass(frozen=True, slots=True)
class ProactiveCheck:
    reason: ProactiveReason
    snapshot: ProactiveSnapshot

    @property
    def eligible(self) -> bool:
        return self.reason is ProactiveReason.ELIGIBLE


class ProactiveEligibility:
    def __init__(
        self,
        contacts: ContactRepository,
        autopilot_state: AutopilotStateRepository,
        sender_state: SenderStateRepository,
        proactive: ProactiveRepository,
        rules: ProactiveSettings,
        schedule: ScheduleService,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self._contacts = contacts
        self._autopilot_state = autopilot_state
        self._sender_state = sender_state
        self._proactive = proactive
        self._rules = rules
        self._schedule = schedule
        self._clock = clock

    @property
    def rules(self) -> ProactiveSettings:
        return self._rules

    async def check(self, contact_id: int) -> ProactiveCheck:
        snapshot = await self.snapshot(contact_id)
        return ProactiveCheck(decide_proactive(snapshot, self._rules), snapshot)

    async def snapshot(self, contact_id: int) -> ProactiveSnapshot:
        now = self._clock()
        tail = await self._proactive.dialogue_tail(contact_id)
        silence_rows = (
            0
            if tail.last_incoming is None
            else await self._proactive.count_proactive_after(contact_id, tail.last_incoming.id)
        )
        return ProactiveSnapshot(
            now=now,
            autopilot=await self._autopilot_state.get(),
            sender=await self._sender_state.get(),
            contact=await self._contacts.get_by_id(contact_id),
            tail=tail,
            silence_rows=silence_rows,
            newest_row_due_at=await self._proactive.newest_proactive_due_at(contact_id),
            daily_rows=await self._proactive.count_proactive_due_since(
                now - timedelta(seconds=PROACTIVE_DAILY_WINDOW_SECONDS)
            ),
            unavailable=await self._schedule.unavailable_soon(now),
        )
