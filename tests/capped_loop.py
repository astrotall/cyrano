from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine

DEFAULT_SLEEP_CAP = 1000


class CappedLoop:
    def __init__(
        self, on_sleep: Callable[[float], Awaitable[bool]], *, cap: int = DEFAULT_SLEEP_CAP
    ) -> None:
        self._on_sleep = on_sleep
        self._cap = cap
        self._sleeps = 0
        self._outcome_reached = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self._sleeps += 1
        if self._sleeps > self._cap:
            raise AssertionError(
                f"the loop went through {self._cap} sleeps without reaching the expected outcome"
            )
        if await self._on_sleep(seconds):
            self._outcome_reached.set()
            await asyncio.Event().wait()

    async def run(self, loop: Coroutine[object, object, None]) -> None:
        running = asyncio.create_task(loop)
        reached = asyncio.create_task(self._outcome_reached.wait())
        try:
            await asyncio.wait({running, reached}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            running.cancel()
            reached.cancel()
        await asyncio.gather(reached, return_exceptions=True)
        with contextlib.suppress(asyncio.CancelledError):
            await running
        if not self._outcome_reached.is_set():
            raise AssertionError("the loop ended before the expected outcome")
