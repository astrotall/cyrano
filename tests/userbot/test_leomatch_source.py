from __future__ import annotations

import asyncio
import logging

import pytest
from telethon.errors import FloodWaitError, UsernameNotOccupiedError
from telethon.tl.types import MessageEntityMentionName, MessageEntityTextUrl, MessageEntityUrl

from cyrano.domain.enums import LeomatchTextKind
from cyrano.domain.models import LeomatchSourceText
from cyrano.services.profile_source import LeomatchSource, ProfileSourceError
from cyrano.userbot.leomatch_source import (
    CARD_WALK_BACK_POSITIONS,
    SCAN_LIMIT,
    SEARCH_RESULT_LIMIT,
    TelethonLeomatchSource,
    entity_text,
    to_chat_message,
)
from tests.userbot.fakes import FakeClient, FakeHistoryMessage, FakeSender

BOT = "leomatchbot"
TG_ID = 5001
OTHER_ID = 6002
USERNAME = "fake_ann"
CARD = "Fakename, 25, Faketown - fake description"
OTHER_CARD = "Othername, 30, Othertown - other description"
MATCH = f"You have a match! @{USERNAME}"
READ_METHODS = {"get_entity", "iter_messages"}


def _client(*messages: FakeHistoryMessage, chat: bool = True) -> FakeClient:
    client = FakeClient()
    client.history = list(messages)
    if chat:
        client.chats[BOT] = FakeSender(id=900, username=BOT)
    return client


def _source(client: FakeClient) -> TelethonLeomatchSource:
    return TelethonLeomatchSource(client, BOT)


def _card(message_id: int, text: str = CARD) -> FakeHistoryMessage:
    return FakeHistoryMessage(id=message_id, raw_text=text)


def _like(message_id: int) -> FakeHistoryMessage:
    return FakeHistoryMessage(id=message_id, raw_text="❤", out=True)


def _match(message_id: int, text: str = MATCH) -> FakeHistoryMessage:
    return FakeHistoryMessage(id=message_id, raw_text=text)


def _mention_match(message_id: int, user_id: int = TG_ID) -> FakeHistoryMessage:
    text = "It is a match with Ann"
    entity = MessageEntityMentionName(offset=21, length=3, user_id=user_id)
    return FakeHistoryMessage(id=message_id, raw_text=text, entities=[entity])


def _by_kind(texts: list[LeomatchSourceText]) -> dict[LeomatchTextKind, list[str]]:
    result: dict[LeomatchTextKind, list[str]] = {}
    for item in texts:
        result.setdefault(item.kind, []).append(item.text)
    return result


def test_the_constants_follow_the_plan() -> None:
    assert (SCAN_LIMIT, SEARCH_RESULT_LIMIT, CARD_WALK_BACK_POSITIONS) == (100, 5, 2)


def test_the_adapter_satisfies_the_service_interface() -> None:
    source: LeomatchSource = TelethonLeomatchSource(FakeClient(), BOT)

    assert source is not None


async def test_a_search_hit_finds_the_match_and_the_card_without_scanning() -> None:
    client = _client(_card(10), _like(11), _match(12))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts) == {LeomatchTextKind.MATCH: [MATCH], LeomatchTextKind.CARD: [CARD]}
    assert client.history_queries == [
        (SEARCH_RESULT_LIMIT, USERNAME, 0),
        (SEARCH_RESULT_LIMIT, f"@{USERNAME}", 0),
        (CARD_WALK_BACK_POSITIONS, None, 12),
    ]


async def test_a_search_miss_falls_back_to_the_local_scan() -> None:
    client = _client(_card(10), _like(11), _mention_match(12))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts)[LeomatchTextKind.MATCH] == ["It is a match with Ann"]
    assert _by_kind(texts)[LeomatchTextKind.CARD] == [CARD]
    assert client.history_queries == [
        (SEARCH_RESULT_LIMIT, USERNAME, 0),
        (SEARCH_RESULT_LIMIT, f"@{USERNAME}", 0),
        (SCAN_LIMIT, None, 0),
        (CARD_WALK_BACK_POSITIONS, None, 12),
    ]


async def test_search_results_that_fail_the_strict_check_are_discarded() -> None:
    client = _client(_match(5, "hello joanna"), _match(6, "joanna again"))

    texts = await _source(client).find_texts(TG_ID, "ann")

    assert texts == []
    assert client.history_queries[0] == (SEARCH_RESULT_LIMIT, "ann", 0)
    assert (SCAN_LIMIT, None, 0) in client.history_queries


async def test_shape_a_the_card_is_two_positions_before_the_match() -> None:
    client = _client(_card(10), _like(11), _match(12))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts)[LeomatchTextKind.CARD] == [CARD]


async def test_shape_b_the_card_is_one_position_before_the_match() -> None:
    client = _client(_like(9), _card(10), _match(11))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts)[LeomatchTextKind.CARD] == [CARD]


async def test_no_card_within_two_positions_returns_only_the_match() -> None:
    client = _client(_card(10), _like(11), _like(12), _match(13))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts) == {LeomatchTextKind.MATCH: [MATCH]}


async def test_a_match_at_the_start_of_the_chat_has_no_card() -> None:
    client = _client(_match(1))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts) == {LeomatchTextKind.MATCH: [MATCH]}


async def test_the_walk_stops_at_someone_elses_match_message() -> None:
    client = _client(_card(10), _mention_match(11, OTHER_ID), _match(12))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts) == {LeomatchTextKind.MATCH: [MATCH]}


async def test_the_walk_stops_at_someone_elses_match_message_behind_the_like() -> None:
    client = _client(_card(9), _match(10, "match with t.me/someone_else"), _like(11), _match(12))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts) == {LeomatchTextKind.MATCH: [MATCH]}


async def test_a_message_with_a_user_link_stops_the_walk() -> None:
    link = MessageEntityTextUrl(offset=0, length=4, url="tg://user?id=77")
    stop = FakeHistoryMessage(id=11, raw_text="Name, 25 - text", entities=[link])
    client = _client(_card(10), stop, _match(12))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts) == {LeomatchTextKind.MATCH: [MATCH]}


async def test_only_one_card_is_ever_taken() -> None:
    client = _client(_card(9, OTHER_CARD), _card(10), _match(11))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts)[LeomatchTextKind.CARD] == [CARD]
    assert len([t for t in texts if t.kind is LeomatchTextKind.CARD]) == 1


async def test_a_card_behind_a_nearer_incoming_text_is_not_taken() -> None:
    client = _client(_card(9, OTHER_CARD), _card(10), _like(11), _match(12))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts)[LeomatchTextKind.CARD] == [CARD]


async def test_several_matches_pick_the_newest_and_its_own_card() -> None:
    client = _client(
        _card(1, OTHER_CARD),
        _match(2, f"old match @{USERNAME}"),
        _card(7),
        _like(8),
        _match(9, f"new match @{USERNAME}"),
    )

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts) == {
        LeomatchTextKind.MATCH: [f"new match @{USERNAME}"],
        LeomatchTextKind.CARD: [CARD],
    }


async def test_a_contact_without_a_username_is_found_by_id_only() -> None:
    client = _client(_card(10), _mention_match(11))

    texts = await _source(client).find_texts(TG_ID, None)

    assert _by_kind(texts)[LeomatchTextKind.CARD] == [CARD]
    assert all(search is None for _, search, _ in client.history_queries)
    assert client.history_queries[0] == (SCAN_LIMIT, None, 0)


@pytest.mark.parametrize("username", ["", "   ", "\t\n", "@", " @ "])
async def test_an_empty_or_whitespace_username_is_never_searched_or_matched(
    username: str,
) -> None:
    client = _client(_card(10), _match(11, "@ann and t.me/ann and ann"))

    texts = await _source(client).find_texts(TG_ID, username)

    assert texts == []
    assert all(search is None for _, search, _ in client.history_queries)


async def test_without_a_username_a_username_only_message_is_not_a_match() -> None:
    client = _client(_card(10), _match(11))

    assert await _source(client).find_texts(TG_ID, None) == []


async def test_with_neither_evidence_the_lookup_returns_nothing_and_no_error() -> None:
    client = _client(_card(10), _match(11, "no evidence at all"))

    assert await _source(client).find_texts(TG_ID, None) == []


async def test_a_caption_only_card_counts_as_text() -> None:
    caption_card = FakeHistoryMessage(id=10, raw_text=CARD, entities=None)
    client = _client(caption_card, _match(11))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts)[LeomatchTextKind.CARD] == [CARD]


async def test_an_incoming_message_without_text_is_skipped_during_the_walk() -> None:
    photo_without_caption = FakeHistoryMessage(id=11, raw_text="")
    client = _client(_card(10), photo_without_caption, _match(12))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts)[LeomatchTextKind.CARD] == [CARD]


async def test_a_message_without_text_none_is_skipped_during_the_walk() -> None:
    client = _client(_card(10), FakeHistoryMessage(id=11, raw_text=None), _match(12))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts)[LeomatchTextKind.CARD] == [CARD]


async def test_the_owners_outgoing_like_is_never_taken_as_a_card() -> None:
    client = _client(_like(10), _match(11))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts) == {LeomatchTextKind.MATCH: [MATCH]}


async def test_an_outgoing_message_with_the_username_is_not_a_match() -> None:
    outgoing = FakeHistoryMessage(id=10, raw_text=f"@{USERNAME}", out=True)
    client = _client(outgoing)

    assert await _source(client).find_texts(TG_ID, USERNAME) == []


async def test_a_chat_that_cannot_be_resolved_returns_nothing_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client(_card(10), _match(11), chat=False)

    with caplog.at_level(logging.INFO):
        texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert texts == []
    assert client.calls == ["get_entity"]
    assert caplog.records == []


async def test_a_username_that_does_not_exist_returns_nothing() -> None:
    client = FakeClient(read_error=UsernameNotOccupiedError(request=None))

    assert await _source(client).find_texts(TG_ID, USERNAME) == []
    assert client.calls == ["get_entity"]


async def test_the_resolved_chat_is_reused() -> None:
    client = _client(_card(10), _match(11))
    source = _source(client)

    await source.find_texts(TG_ID, USERNAME)
    await source.find_texts(TG_ID, USERNAME)

    assert client.calls.count("get_entity") == 1


async def test_a_flood_wait_fails_the_lookup_with_a_source_error_without_waiting() -> None:
    client = FakeClient(read_error=FloodWaitError(request=None, capture=3600))

    async with asyncio.timeout(1):
        with pytest.raises(ProfileSourceError, match="FloodWaitError"):
            await _source(client).find_texts(TG_ID, USERNAME)


async def test_only_read_methods_are_used() -> None:
    client = _client(_card(10), _like(11), _match(12))

    await _source(client).find_texts(TG_ID, USERNAME)
    await _source(client).find_texts(TG_ID, None)

    assert client.write_attempts == []
    assert set(client.calls) <= READ_METHODS


async def test_the_search_asks_for_at_most_five_results_per_query() -> None:
    matches = [_match(number, f"match {number} @{USERNAME}") for number in range(1, 9)]
    client = _client(*matches)

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts)[LeomatchTextKind.MATCH] == [f"match 8 @{USERNAME}"]
    assert all(
        limit == SEARCH_RESULT_LIMIT for limit, search, _ in client.history_queries if search
    )


def test_entity_text_counts_utf16_units_for_text_after_an_emoji() -> None:
    text = "\U0001f338 t.me/fake_ann"

    assert entity_text(text, 3, 13) == "t.me/fake_ann"


def test_entity_text_survives_offsets_outside_the_text() -> None:
    assert entity_text("abc", 10, 5) == ""
    assert entity_text("", 0, 5) == ""


def test_the_conversion_reads_mentions_text_urls_and_plain_urls() -> None:
    text = "Ann see https://t.me/fake_ann or here"
    message = FakeHistoryMessage(
        id=4,
        raw_text=text,
        entities=[
            MessageEntityMentionName(offset=0, length=3, user_id=TG_ID),
            MessageEntityUrl(offset=8, length=21),
            MessageEntityTextUrl(offset=33, length=4, url="tg://user?id=77"),
        ],
    )

    chat_message = to_chat_message(message)

    assert chat_message.id == 4
    assert chat_message.out is False
    assert chat_message.mentioned_user_ids == (TG_ID,)
    assert chat_message.links == ("https://t.me/fake_ann", "tg://user?id=77")


def test_the_conversion_handles_missing_text_and_entities() -> None:
    message = FakeHistoryMessage(id=4, raw_text=None, out=None, entities=None)

    chat_message = to_chat_message(message)

    assert (chat_message.text, chat_message.out, chat_message.links) == ("", False, ())


async def test_a_url_entity_after_an_emoji_is_matched() -> None:
    text = f"\U0001f338 t.me/{USERNAME}"
    entity = MessageEntityUrl(offset=3, length=len(f"t.me/{USERNAME}"))
    client = _client(FakeHistoryMessage(id=5, raw_text=text, entities=[entity]))

    texts = await _source(client).find_texts(TG_ID, USERNAME)

    assert _by_kind(texts) == {LeomatchTextKind.MATCH: [text]}
