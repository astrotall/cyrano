from __future__ import annotations

import pytest

from cyrano.domain.enums import MessageDirection
from cyrano.domain.models import Contact
from cyrano.reply_cli import ReplyPreviewCommand, ReplyPreviewRequest
from cyrano.services.gender import GenderResolver
from tests.privacy import assert_absent
from tests.reply_db import PERSONA_MARKER, Repositories
from tests.services.fakes import ScriptedLLMClient

MODEL = "fake-model"
INCOMING_ONE_ZQ = "preview-quote-incoming-one-zq invented"
OUTGOING_ZQ = "preview-quote-outgoing-zq invented"
INCOMING_TWO_ZQ = "preview-quote-incoming-two-zq invented"
TG_MESSAGE_IDS = (733000000101, 733000000102, 733000000103)


def _command(repos: Repositories, reply: str, *, quote_enabled: bool = True) -> ReplyPreviewCommand:
    return ReplyPreviewCommand(
        repos.generator(ScriptedLLMClient(reply), quote_enabled=quote_enabled),
        MODEL,
        repos.contacts,
        GenderResolver(),
    )


async def _contact(repos: Repositories) -> Contact:
    await repos.personas.replace(PERSONA_MARKER)
    contact = await repos.contact()
    texts = (
        (MessageDirection.INCOMING, INCOMING_ONE_ZQ),
        (MessageDirection.OUTGOING, OUTGOING_ZQ),
        (MessageDirection.INCOMING, INCOMING_TWO_ZQ),
    )
    for (direction, text), number in zip(texts, TG_MESSAGE_IDS, strict=True):
        await repos.say(contact.id, direction, text, number)
    return contact


async def _lines(
    repos: Repositories,
    reply: str,
    capsys: pytest.CaptureFixture[str],
    *,
    quote_enabled: bool = True,
) -> list[str]:
    contact = await _contact(repos)
    code = await _command(repos, reply, quote_enabled=quote_enabled).run(
        ReplyPreviewRequest(contact.id)
    )
    captured = capsys.readouterr()
    assert code == 0
    for number in TG_MESSAGE_IDS:
        assert_absent(number, captured.out + captured.err)
    return captured.out.splitlines()


async def test_the_quoted_part_shows_its_dialogue_number(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = await _lines(repos, "sure\n↩1 the walk was fun", capsys)

    assert lines[:2] == ["[1/2] sure", "[2/2 ↩1] the walk was fun"]


async def test_a_single_quoted_part_is_numbered_too(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = await _lines(repos, "↩3: yes", capsys)

    assert lines[0] == "[1/1 ↩3] yes"


async def test_a_reply_without_a_target_prints_exactly_as_before(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    single = await _lines(repos, "fine", capsys)

    assert single[0] == "fine"
    assert len(single) == 3


async def test_several_parts_without_a_target_print_exactly_as_before(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = await _lines(repos, "one\ntwo", capsys)

    assert lines[:2] == ["[1/2] one", "[2/2] two"]


async def test_a_marker_for_our_own_message_prints_the_plain_part(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = await _lines(repos, "↩2 yes", capsys)

    assert lines[0] == "yes"


async def test_the_switch_off_prints_no_target(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = await _lines(repos, "one\n↩1 two", capsys, quote_enabled=False)

    assert lines[:2] == ["[1/2] one", "[2/2] two"]
