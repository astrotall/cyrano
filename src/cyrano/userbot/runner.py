"""Lifecycle of the userbot: register, connect, catch up, listen, stop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Coroutine

from cyrano.userbot.client import connect_authorized, disconnect_client
from cyrano.userbot.errors import Disconnected
from cyrano.userbot.handler import IncomingHandler, IncomingProcessor
from cyrano.userbot.protocols import UserbotClient

logger = logging.getLogger("cyrano.userbot")


class StartInterrupted(Exception):
    pass


async def _wait_for_disconnect(client: UserbotClient) -> None:
    with contextlib.suppress(Exception):
        await client.disconnected


async def _until_stopped[T](work: Coroutine[object, object, T], stop: asyncio.Event) -> T:
    stopped = asyncio.create_task(stop.wait())
    working = asyncio.create_task(work)
    try:
        await asyncio.wait({stopped, working}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()
        working.cancel()
        await asyncio.gather(stopped, working, return_exceptions=True)
    if working.cancelled():
        raise StartInterrupted
    return working.result()


class Userbot:
    def __init__(self, client: UserbotClient, intake: IncomingProcessor) -> None:
        self._client = client
        self._handler = IncomingHandler(intake)

    async def start(self, stop: asyncio.Event) -> None:
        self._handler.register(self._client)
        try:
            await _until_stopped(connect_authorized(self._client), stop)
            await _until_stopped(self._client.catch_up(), stop)
        except StartInterrupted:
            logger.info("userbot start interrupted by stop signal")
            return
        logger.info("userbot listening")

    async def run_until(self, stop: asyncio.Event) -> None:
        stopped = asyncio.create_task(stop.wait())
        disconnected = asyncio.create_task(_wait_for_disconnect(self._client))
        try:
            await asyncio.wait({stopped, disconnected}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (stopped, disconnected):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if not stop.is_set():
            raise Disconnected

    async def stop(self) -> None:
        self._handler.unregister(self._client)
        await disconnect_client(self._client)
        logger.info(
            "userbot stopped",
            extra={
                "handled": self._handler.handled,
                "rejected": self._handler.rejected,
                "failed": self._handler.failed,
            },
        )
