from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from cyrano.domain.enums import LimitScope, LimitWindow
from cyrano.domain.outbox import LIMIT_WINDOW_SECONDS, SendLimit
from cyrano.services.repositories import MessageRepository


class LimitSettings(Protocol):
    @property
    def send_limit_minute(self) -> int: ...

    @property
    def send_limit_hour(self) -> int: ...

    @property
    def send_limit_day(self) -> int: ...

    @property
    def send_limit_contact_minute(self) -> int: ...

    @property
    def send_limit_contact_hour(self) -> int: ...

    @property
    def send_limit_contact_day(self) -> int: ...


class LimitDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    free_at: datetime | None = None
    reached: tuple[SendLimit, ...] = ()


class LimitUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    limit: SendLimit
    used: int
    contact_id: int | None = None


def window_of(limit: SendLimit) -> timedelta:
    return timedelta(seconds=LIMIT_WINDOW_SECONDS[limit.window])


def slot_frees_at(limit: SendLimit, times: Sequence[datetime], now: datetime) -> datetime | None:
    if len(times) < limit.maximum:
        return None
    frees_at = times[limit.maximum - 1] + window_of(limit)
    return frees_at if frees_at > now else None


def decide(samples: Iterable[tuple[SendLimit, Sequence[datetime]]], now: datetime) -> LimitDecision:
    reached: list[SendLimit] = []
    free_at: datetime | None = None
    for limit, times in samples:
        frees_at = slot_frees_at(limit, times, now)
        if frees_at is not None:
            reached.append(limit)
            free_at = frees_at if free_at is None else max(free_at, frees_at)
    return LimitDecision(free_at=free_at, reached=tuple(reached))


def limits_from(settings: LimitSettings) -> tuple[SendLimit, ...]:
    return (
        SendLimit(
            scope=LimitScope.GLOBAL, window=LimitWindow.MINUTE, maximum=settings.send_limit_minute
        ),
        SendLimit(
            scope=LimitScope.GLOBAL, window=LimitWindow.HOUR, maximum=settings.send_limit_hour
        ),
        SendLimit(scope=LimitScope.GLOBAL, window=LimitWindow.DAY, maximum=settings.send_limit_day),
        SendLimit(
            scope=LimitScope.CONTACT,
            window=LimitWindow.MINUTE,
            maximum=settings.send_limit_contact_minute,
        ),
        SendLimit(
            scope=LimitScope.CONTACT,
            window=LimitWindow.HOUR,
            maximum=settings.send_limit_contact_hour,
        ),
        SendLimit(
            scope=LimitScope.CONTACT,
            window=LimitWindow.DAY,
            maximum=settings.send_limit_contact_day,
        ),
    )


class SendLimiter:
    def __init__(self, messages: MessageRepository, settings: LimitSettings) -> None:
        self._messages = messages
        self._limits = limits_from(settings)

    async def check(self, contact_id: int, now: datetime) -> LimitDecision:
        samples = [(limit, await self._times(limit, contact_id, now)) for limit in self._limits]
        return decide(samples, now)

    async def usage(self, now: datetime) -> list[LimitUsage]:
        return [await self._usage_of(limit, now) for limit in self._limits]

    async def _times(self, limit: SendLimit, contact_id: int, now: datetime) -> list[datetime]:
        return await self._messages.outgoing_times(
            now - window_of(limit),
            limit.maximum,
            contact_id if limit.scope is LimitScope.CONTACT else None,
        )

    async def _usage_of(self, limit: SendLimit, now: datetime) -> LimitUsage:
        start = now - window_of(limit)
        if limit.scope is LimitScope.CONTACT:
            peak = await self._messages.busiest_outgoing_contact(start)
            if peak is None:
                return LimitUsage(limit=limit, used=0)
            return LimitUsage(limit=limit, used=peak.count, contact_id=peak.contact_id)
        times = await self._messages.outgoing_times(start, limit.maximum)
        return LimitUsage(limit=limit, used=sum(moment > start for moment in times))
