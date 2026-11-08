from __future__ import annotations

import dataclasses
import logging

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from cyrano.config import Settings
from cyrano.domain.autopilot import QUEUE_CONNECT_RETRIES, REPLY_JOB_NAME, reply_job_id
from cyrano.domain.proactive import PROACTIVE_JOB_NAME, proactive_job_id
from cyrano.domain.summary import SUMMARY_JOB_NAME, summary_job_id

logger = logging.getLogger("cyrano.worker.queue")


def redis_settings(settings: Settings) -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url.get_secret_value())


class ArqReplyJobQueue:
    def __init__(self, settings: Settings) -> None:
        self._redis_settings = dataclasses.replace(
            redis_settings(settings), conn_retries=QUEUE_CONNECT_RETRIES
        )
        self._pool: ArqRedis | None = None

    async def start(self) -> None:
        try:
            await self._connected()
        except Exception as exc:
            logger.error("reply job queue is not connected", extra={"error": type(exc).__name__})

    async def enqueue(self, contact_id: int, trigger_message_id: int, defer_seconds: int) -> None:
        pool = await self._connected()
        await pool.enqueue_job(
            REPLY_JOB_NAME,
            contact_id,
            trigger_message_id,
            _job_id=reply_job_id(contact_id, trigger_message_id),
            _defer_by=defer_seconds,
        )

    async def pool(self) -> ArqRedis:
        return await self._connected()

    async def close(self) -> None:
        if self._pool is not None:
            pool, self._pool = self._pool, None
            await pool.aclose()

    async def _connected(self) -> ArqRedis:
        if self._pool is None:
            self._pool = await create_pool(self._redis_settings)
        return self._pool


class ArqSummaryJobQueue:
    def __init__(self, connection: ArqReplyJobQueue) -> None:
        self._connection = connection

    async def enqueue(self, contact_id: int, up_to_message_id: int) -> None:
        pool = await self._connection.pool()
        await pool.enqueue_job(
            SUMMARY_JOB_NAME,
            contact_id,
            up_to_message_id,
            _job_id=summary_job_id(contact_id, up_to_message_id),
        )


class ArqProactiveJobQueue:
    def __init__(self, connection: ArqReplyJobQueue) -> None:
        self._connection = connection

    async def enqueue(self, contact_id: int, last_message_id: int) -> None:
        pool = await self._connection.pool()
        await pool.enqueue_job(
            PROACTIVE_JOB_NAME,
            contact_id,
            last_message_id,
            _job_id=proactive_job_id(contact_id, last_message_id),
        )
