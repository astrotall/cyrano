from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cyrano.db.repositories import SqlSummaryRepository
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import MessageDirection
from cyrano.domain.summary import ContactSummary
from cyrano.prompts.texts import SUMMARY_HEADER, TOLD_ABOUT_OWNER_NOTE
from cyrano.services.summary_sweeper import SummarySweeper
from cyrano.services.summary_updater import SummaryUpdater
from cyrano.worker.jobs import SUMMARY_UPDATER_KEY, update_contact_summary
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.reply_db import (
    BIO_MARKER,
    MESSAGE_MARKER,
    PERSONA_MARKER,
    USERNAME_MARKER,
    Repositories,
)
from tests.services.autopilot_rig import json_logs
from tests.services.fakes import ScriptedLLMClient
from tests.services.summary_fakes import ScriptedJsonClient

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
VALUE_MARKER = "e2e-summary-value-zq"
TOLD_MARKER = "e2e-told-marker-zq"
EVERY = 12


class _InlineQueue:
    def __init__(self, updater: SummaryUpdater) -> None:
        self.ctx: dict[str, object] = {SUMMARY_UPDATER_KEY: updater}
        self.results: list[str] = []

    async def enqueue(self, contact_id: int, up_to_message_id: int) -> None:
        self.results.append(await update_contact_summary(self.ctx, contact_id, up_to_message_id))


async def test_a_due_contact_is_summarised_and_the_next_reply_prompt_carries_the_memory(
    repos: Repositories, session_factory: SessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    await repos.personas.replace(PERSONA_MARKER)
    contact = await repos.contact(mode="default")
    for number in range(1, EVERY + 1):
        direction = MessageDirection.INCOMING if number % 2 else MessageDirection.OUTGOING
        await repos.say(contact.id, direction, f"{MESSAGE_MARKER} {number}", number)
    summaries = SqlSummaryRepository(session_factory)
    client = ScriptedJsonClient(ContactSummary(name=VALUE_MARKER, told_about_owner=[TOLD_MARKER]))
    updater = SummaryUpdater(
        contacts=repos.contacts, summaries=summaries, client=client, clock=lambda: NOW
    )
    queue = _InlineQueue(updater)
    sweeper = SummarySweeper(
        summaries, queue, enabled=True, every_messages=EVERY, clock=lambda: NOW
    )

    assert await sweeper.sweep() == 1
    assert queue.results == ["updated"]
    assert await sweeper.sweep() == 0

    reply_client = ScriptedLLMClient("fine")
    await repos.generator(reply_client).generate(contact.id)
    system = reply_client.calls[0][0][0].content
    assert SUMMARY_HEADER in system
    assert f"{TOLD_ABOUT_OWNER_NOTE}\n- {TOLD_MARKER}" in system

    output = json_logs(caplog)
    assert "summary job finished" in output
    for marker in (
        VALUE_MARKER,
        TOLD_MARKER,
        MESSAGE_MARKER,
        PERSONA_MARKER,
        BIO_MARKER,
        USERNAME_MARKER,
        "Contactfirst",
        FAKE_TELEGRAM_ID,
    ):
        assert_absent(marker, output)
