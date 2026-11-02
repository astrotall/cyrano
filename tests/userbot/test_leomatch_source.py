from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from telethon.errors import FloodWaitError, UsernameNotOccupiedError
from telethon.tl.types import MessageEntityMentionName, MessageEntityTextUrl, MessageEntityUrl

from cyrano.domain.models import MAX_CARD_PHOTOS
from cyrano.services.profile_source import LeomatchLookup, LeomatchSource, ProfileSourceError
from cyrano.userbot.leomatch_source import (
    CARD_HISTORY_LIMIT,
    CARD_WALK_BACK_POSITIONS,
    SCAN_LIMIT,
    SEARCH_RESULT_LIMIT,
    UTF16,
    UTF16_UNIT_BYTES,
    TelethonCardPhotos,
    TelethonLeomatchSource,
    entity_text,
    group_positions,
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
READ_METHODS = {"get_entity", "iter_messages", "download_media"}
FAKE_PHOTO_OBJECT = object()


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


def _photo(
    message_id: int, grouped_id: int | None, caption: str = "", content: bytes | None = None
) -> FakeHistoryMessage:
    return FakeHistoryMessage(
        id=message_id,
        raw_text=caption,
        photo=FAKE_PHOTO_OBJECT,
        grouped_id=grouped_id,
        media_bytes=content if content is not None else f"photo-{message_id}".encode(),
    )


def _video(
    message_id: int, caption: str = CARD, grouped_id: int | None = None
) -> FakeHistoryMessage:
    return FakeHistoryMessage(id=message_id, raw_text=caption, grouped_id=grouped_id)


def _album(
    first_id: int, captions: tuple[str, ...], grouped_id: int = 700
) -> list[FakeHistoryMessage]:
    return [
        _photo(first_id + offset, grouped_id, caption) for offset, caption in enumerate(captions)
    ]


def _mention_match(message_id: int, user_id: int = TG_ID) -> FakeHistoryMessage:
    text = "It is a match with Ann"
    entity = MessageEntityMentionName(offset=21, length=3, user_id=user_id)
    return FakeHistoryMessage(id=message_id, raw_text=text, entities=[entity])


async def _texts(client: FakeClient, username: str | None = USERNAME) -> LeomatchLookup:
    return await _source(client).find_texts(TG_ID, username)


def test_the_constants_follow_the_plan() -> None:
    assert (SCAN_LIMIT, SEARCH_RESULT_LIMIT, CARD_WALK_BACK_POSITIONS) == (100, 5, 2)
    assert (CARD_HISTORY_LIMIT, MAX_CARD_PHOTOS) == (15, 5)


def test_the_adapter_satisfies_the_service_interface() -> None:
    source: LeomatchSource = TelethonLeomatchSource(FakeClient(), BOT)

    assert source is not None


async def test_a_search_hit_finds_the_match_and_the_card_without_scanning() -> None:
    client = _client(_card(10), _like(11), _match(12))

    lookup = await _texts(client)

    assert (lookup.card_text, lookup.match_text) == (CARD, MATCH)
    assert client.history_queries == [
        (SEARCH_RESULT_LIMIT, USERNAME, 0),
        (SEARCH_RESULT_LIMIT, f"@{USERNAME}", 0),
        (CARD_HISTORY_LIMIT, None, 12),
    ]


async def test_a_search_miss_falls_back_to_the_local_scan() -> None:
    client = _client(_card(10), _like(11), _mention_match(12))

    lookup = await _texts(client)

    assert (lookup.card_text, lookup.match_text) == (CARD, "It is a match with Ann")
    assert client.history_queries == [
        (SEARCH_RESULT_LIMIT, USERNAME, 0),
        (SEARCH_RESULT_LIMIT, f"@{USERNAME}", 0),
        (SCAN_LIMIT, None, 0),
        (CARD_HISTORY_LIMIT, None, 12),
    ]


async def test_search_results_that_fail_the_strict_check_are_discarded() -> None:
    client = _client(_match(5, "hello joanna"), _match(6, "joanna again"))

    lookup = await _texts(client, "ann")

    assert lookup == LeomatchLookup()
    assert client.history_queries[0] == (SEARCH_RESULT_LIMIT, "ann", 0)
    assert (SCAN_LIMIT, None, 0) in client.history_queries


async def test_shape_a_the_card_is_two_positions_before_the_match() -> None:
    client = _client(_card(10), _like(11), _match(12))

    assert (await _texts(client)).card_text == CARD


async def test_shape_b_the_card_is_one_position_before_the_match() -> None:
    client = _client(_like(9), _card(10), _match(11))

    assert (await _texts(client)).card_text == CARD


async def test_no_card_within_two_positions_returns_only_the_match() -> None:
    client = _client(_card(10), _like(11), _like(12), _match(13))

    lookup = await _texts(client)

    assert (lookup.card_text, lookup.match_text, lookup.card_photos) == (None, MATCH, None)


async def test_a_match_at_the_start_of_the_chat_has_no_card() -> None:
    client = _client(_match(1))

    lookup = await _texts(client)

    assert (lookup.card_text, lookup.match_text) == (None, MATCH)


async def test_the_walk_stops_at_someone_elses_match_message() -> None:
    client = _client(_card(10), _mention_match(11, OTHER_ID), _match(12))

    lookup = await _texts(client)

    assert (lookup.card_text, lookup.match_text) == (None, MATCH)


async def test_the_walk_stops_at_someone_elses_match_message_behind_the_like() -> None:
    client = _client(_card(9), _match(10, "match with t.me/someone_else"), _like(11), _match(12))

    lookup = await _texts(client)

    assert (lookup.card_text, lookup.match_text) == (None, MATCH)


async def test_a_message_with_a_user_link_stops_the_walk() -> None:
    link = MessageEntityTextUrl(offset=0, length=4, url="tg://user?id=77")
    stop = FakeHistoryMessage(id=11, raw_text="Name, 25 - text", entities=[link])
    client = _client(_card(10), stop, _match(12))

    assert (await _texts(client)).card_text is None


async def test_only_one_card_is_ever_taken() -> None:
    client = _client(_card(9, OTHER_CARD), _card(10), _match(11))

    assert (await _texts(client)).card_text == CARD


async def test_a_card_behind_a_nearer_incoming_text_is_not_taken() -> None:
    client = _client(_card(9, OTHER_CARD), _card(10), _like(11), _match(12))

    assert (await _texts(client)).card_text == CARD


async def test_several_matches_pick_the_newest_and_its_own_card() -> None:
    client = _client(
        _card(1, OTHER_CARD),
        _match(2, f"old match @{USERNAME}"),
        _card(7),
        _like(8),
        _match(9, f"new match @{USERNAME}"),
    )

    lookup = await _texts(client)

    assert (lookup.card_text, lookup.match_text) == (CARD, f"new match @{USERNAME}")


async def test_a_contact_without_a_username_is_found_by_id_only() -> None:
    client = _client(_card(10), _mention_match(11))

    lookup = await _texts(client, None)

    assert lookup.card_text == CARD
    assert all(search is None for _, search, _ in client.history_queries)
    assert client.history_queries[0] == (SCAN_LIMIT, None, 0)


@pytest.mark.parametrize("username", ["", "   ", "\t\n", "@", " @ "])
async def test_an_empty_or_whitespace_username_is_never_searched_or_matched(
    username: str,
) -> None:
    client = _client(_card(10), _match(11, "@ann and t.me/ann and ann"))

    lookup = await _texts(client, username)

    assert lookup == LeomatchLookup()
    assert all(search is None for _, search, _ in client.history_queries)


async def test_without_a_username_a_username_only_message_is_not_a_match() -> None:
    client = _client(_card(10), _match(11))

    assert await _texts(client, None) == LeomatchLookup()


async def test_with_neither_evidence_the_lookup_returns_nothing_and_no_error() -> None:
    client = _client(_card(10), _match(11, "no evidence at all"))

    assert await _texts(client, None) == LeomatchLookup()


async def test_a_caption_only_card_counts_as_text() -> None:
    client = _client(FakeHistoryMessage(id=10, raw_text=CARD, entities=None), _match(11))

    assert (await _texts(client)).card_text == CARD


async def test_an_incoming_message_without_text_is_skipped_during_the_walk() -> None:
    client = _client(_card(10), FakeHistoryMessage(id=11, raw_text=""), _match(12))

    assert (await _texts(client)).card_text == CARD


async def test_a_message_without_text_none_is_skipped_during_the_walk() -> None:
    client = _client(_card(10), FakeHistoryMessage(id=11, raw_text=None), _match(12))

    assert (await _texts(client)).card_text == CARD


async def test_the_owners_outgoing_like_is_never_taken_as_a_card() -> None:
    client = _client(_like(10), _match(11))

    lookup = await _texts(client)

    assert (lookup.card_text, lookup.match_text) == (None, MATCH)


async def test_an_outgoing_message_with_the_username_is_not_a_match() -> None:
    outgoing = FakeHistoryMessage(id=10, raw_text=f"@{USERNAME}", out=True)

    assert await _texts(_client(outgoing)) == LeomatchLookup()


async def test_a_chat_that_cannot_be_resolved_returns_nothing_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client(_card(10), _match(11), chat=False)

    with caplog.at_level(logging.INFO):
        lookup = await _texts(client)

    assert lookup == LeomatchLookup()
    assert client.calls == ["get_entity"]
    assert caplog.records == []


async def test_a_username_that_does_not_exist_returns_nothing() -> None:
    client = FakeClient(read_error=UsernameNotOccupiedError(request=None))

    assert await _texts(client) == LeomatchLookup()
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
            await _texts(client)


async def test_only_read_methods_are_used() -> None:
    client = _client(_card(10), _like(11), _match(12))

    await _texts(client)
    await _texts(client, None)

    assert client.write_attempts == []
    assert set(client.calls) <= READ_METHODS


async def test_the_search_asks_for_at_most_five_results_per_query() -> None:
    matches = [_match(number, f"match {number} @{USERNAME}") for number in range(1, 9)]
    client = _client(*matches)

    lookup = await _texts(client)

    assert lookup.match_text == f"match 8 @{USERNAME}"
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

    lookup = await _texts(client)

    assert (lookup.match_text, lookup.card_text) == (text, None)


OBSERVED_HEADER = "Кому-то понравилась твоя анкета:"
OBSERVED_CARD = f"{OBSERVED_HEADER}\n\nАня, 18, Москва"
OBSERVED_MATCH = "Отлично! Надеюсь хорошо проведете время 🙌\n\nНачинай общаться 👉 Аня"


def _observed_match(message_id: int) -> FakeHistoryMessage:
    units = len(OBSERVED_MATCH.encode(UTF16, errors="surrogatepass")) // UTF16_UNIT_BYTES
    entity = MessageEntityMentionName(offset=units - 3, length=3, user_id=TG_ID)
    return FakeHistoryMessage(id=message_id, raw_text=OBSERVED_MATCH, entities=[entity])


async def _download_all(lookup: LeomatchLookup, directory: Path) -> list[bytes]:
    assert lookup.card_photos is not None
    contents: list[bytes] = []
    for index in range(lookup.card_photos.count):
        destination = directory / f"photo-{index}.jpg"
        assert await lookup.card_photos.download(index, destination) is True
        contents.append(destination.read_bytes())
    return contents


async def test_the_observed_chain_is_found_by_the_user_link_without_a_username() -> None:
    client = _client(_photo(10, None, OBSERVED_CARD), _observed_match(11))

    lookup = await _texts(client, None)

    assert (lookup.card_text, lookup.match_text) == (OBSERVED_CARD, OBSERVED_MATCH)
    assert lookup.match_name == "Аня"
    assert lookup.card_photos is not None
    assert lookup.card_photos.count == 1


@pytest.mark.parametrize("caption_position", [0, 1, 2])
async def test_an_album_of_three_gives_its_caption_wherever_it_sits(
    caption_position: int,
) -> None:
    captions = tuple(OBSERVED_CARD if i == caption_position else "" for i in range(3))
    client = _client(*_album(10, captions), _observed_match(13))

    lookup = await _texts(client, None)

    assert lookup.card_text == OBSERVED_CARD
    assert lookup.card_photos is not None
    assert lookup.card_photos.count == 3


async def test_the_photos_of_an_album_are_ordered_by_message_id(tmp_path: Path) -> None:
    album = _album(10, (OBSERVED_CARD, "", ""))
    client = _client(*reversed(album), _observed_match(13))

    lookup = await _texts(client, None)

    assert await _download_all(lookup, tmp_path) == [b"photo-10", b"photo-11", b"photo-12"]
    assert [message_id for message_id, _ in client.media_downloads] == [10, 11, 12]


async def test_an_album_of_one_is_a_card_with_one_photo(tmp_path: Path) -> None:
    client = _client(_photo(10, 700, OBSERVED_CARD), _observed_match(11))

    lookup = await _texts(client, None)

    assert lookup.card_text == OBSERVED_CARD
    assert await _download_all(lookup, tmp_path) == [b"photo-10"]


async def test_a_plain_single_message_card_with_a_photo(tmp_path: Path) -> None:
    client = _client(_photo(10, None, OBSERVED_CARD), _observed_match(11))

    lookup = await _texts(client, None)

    assert await _download_all(lookup, tmp_path) == [b"photo-10"]


async def test_a_video_only_card_gives_its_caption_and_no_photos() -> None:
    client = _client(_video(10, OBSERVED_CARD), _observed_match(11))

    lookup = await _texts(client, None)

    assert lookup.card_text == OBSERVED_CARD
    assert lookup.card_photos is None
    assert client.media_downloads == []


async def test_a_video_is_never_downloaded_when_it_sits_in_a_group_with_photos(
    tmp_path: Path,
) -> None:
    group = [_photo(10, 700, OBSERVED_CARD), _video(11, "", 700)]
    client = _client(*group, _observed_match(12))

    lookup = await _texts(client, None)

    assert await _download_all(lookup, tmp_path) == [b"photo-10"]
    assert [message_id for message_id, _ in client.media_downloads] == [10]


async def test_an_album_counts_as_one_position_in_shape_a(tmp_path: Path) -> None:
    client = _client(*_album(10, (OBSERVED_CARD, "", "")), _like(13), _observed_match(14))

    lookup = await _texts(client, None)

    assert lookup.card_text == OBSERVED_CARD
    assert await _download_all(lookup, tmp_path) == [b"photo-10", b"photo-11", b"photo-12"]


async def test_an_album_counts_as_one_position_in_shape_b() -> None:
    client = _client(*_album(10, ("", "", OBSERVED_CARD)), _observed_match(13))

    lookup = await _texts(client, None)

    assert lookup.card_text == OBSERVED_CARD


async def test_an_album_without_any_caption_gives_no_card_and_no_photos() -> None:
    client = _client(*_album(10, ("", "", "")), _observed_match(13))

    lookup = await _texts(client, None)

    assert (lookup.card_text, lookup.card_photos) == (None, None)
    assert lookup.match_text == OBSERVED_MATCH


async def test_a_captionless_album_is_skipped_for_the_card_behind_it() -> None:
    client = _client(_card(8, OBSERVED_CARD), *_album(10, ("", "", "")), _observed_match(13))

    lookup = await _texts(client, None)

    assert lookup.card_text == OBSERVED_CARD
    assert lookup.card_photos is None


async def test_a_card_behind_a_captionless_album_and_a_like_is_out_of_reach() -> None:
    client = _client(
        _card(7, OBSERVED_CARD), *_album(8, ("", "", "")), _like(11), _observed_match(12)
    )

    assert (await _texts(client, None)).card_text is None


async def test_the_guard_applies_to_any_message_of_a_position() -> None:
    link = MessageEntityTextUrl(offset=0, length=4, url="tg://user?id=77")
    album = [
        _photo(10, 700, OBSERVED_CARD),
        _photo(11, 700, ""),
        FakeHistoryMessage(
            id=12, raw_text="Name", entities=[link], photo=FAKE_PHOTO_OBJECT, grouped_id=700
        ),
    ]
    client = _client(*album, _observed_match(13))

    lookup = await _texts(client, None)

    assert (lookup.card_text, lookup.card_photos) == (None, None)


async def test_the_walk_stops_at_someone_elses_match_inside_a_position() -> None:
    other = FakeHistoryMessage(
        id=11,
        raw_text="match with t.me/someone_else",
        entities=None,
        photo=FAKE_PHOTO_OBJECT,
        grouped_id=701,
    )
    client = _client(_card(9, OBSERVED_CARD), other, _observed_match(12))

    lookup = await _texts(client, None)

    assert (lookup.card_text, lookup.card_photos) == (None, None)


async def test_at_most_two_positions_are_walked_back() -> None:
    client = _client(
        _card(5, OBSERVED_CARD), *_album(6, ("", "", "")), _like(9), _observed_match(10)
    )

    assert (await _texts(client, None)).card_text is None


async def test_the_nearest_incoming_text_wins_over_an_older_album() -> None:
    client = _client(*_album(2, (OTHER_CARD, "", "")), _card(5, OBSERVED_CARD), _observed_match(6))

    lookup = await _texts(client, None)

    assert lookup.card_text == OBSERVED_CARD
    assert lookup.card_photos is None


async def test_at_most_five_photos_are_offered(tmp_path: Path) -> None:
    album = [_photo(10 + n, 700, OBSERVED_CARD if n == 0 else "") for n in range(7)]
    client = _client(*album, _observed_match(20))

    lookup = await _texts(client, None)

    assert lookup.card_photos is not None
    assert lookup.card_photos.count == MAX_CARD_PHOTOS
    contents = await _download_all(lookup, tmp_path)
    assert contents == [f"photo-{10 + n}".encode() for n in range(MAX_CARD_PHOTOS)]


async def test_two_neighbouring_albums_stay_two_positions() -> None:
    first = _album(2, (OTHER_CARD, "", ""), grouped_id=701)
    second = _album(5, (OBSERVED_CARD, "", ""), grouped_id=702)
    client = _client(*first, *second, _observed_match(8))

    lookup = await _texts(client, None)

    assert lookup.card_text == OBSERVED_CARD
    assert lookup.card_photos is not None
    assert lookup.card_photos.count == 3


async def test_nothing_is_downloaded_before_the_collector_asks() -> None:
    client = _client(*_album(10, (OBSERVED_CARD, "", "")), _observed_match(13))

    await _texts(client, None)

    assert client.media_downloads == []


async def test_a_photo_on_the_match_message_is_not_a_card_photo() -> None:
    match = _observed_match(11)
    match.photo = FAKE_PHOTO_OBJECT
    client = _client(_card(10, OBSERVED_CARD), match)

    assert (await _texts(client, None)).card_photos is None


async def test_a_photo_on_an_older_message_is_not_taken_for_the_card() -> None:
    client = _client(_photo(9, None, OBSERVED_CARD), _card(10, OBSERVED_CARD), _observed_match(11))

    lookup = await _texts(client, None)

    assert lookup.card_text == OBSERVED_CARD
    assert lookup.card_photos is None


async def test_no_match_gives_an_empty_lookup() -> None:
    client = _client(_photo(10, None, OBSERVED_CARD))

    lookup = await _texts(client, None)

    assert lookup == LeomatchLookup()


async def test_the_match_name_is_the_text_under_the_user_link_entity() -> None:
    client = _client(_card(10, OBSERVED_CARD), _observed_match(11))

    assert (await _texts(client, None)).match_name == "Аня"


async def test_the_match_name_is_read_from_a_tg_user_url_entity() -> None:
    text = "Congratulations! Start 👉 Linkname"
    start = len(text) - len("Linkname")
    units = len(text[:start].encode(UTF16, errors="surrogatepass")) // UTF16_UNIT_BYTES
    entity = MessageEntityTextUrl(offset=units, length=len("Linkname"), url=f"tg://user?id={TG_ID}")
    match = FakeHistoryMessage(id=11, raw_text=text, entities=[entity])
    client = _client(_card(10, OBSERVED_CARD), match)

    assert (await _texts(client, None)).match_name == "Linkname"


async def test_the_match_name_ignores_links_to_other_users() -> None:
    text = "Congratulations! Start 👉 Someone"
    entity = MessageEntityMentionName(offset=25, length=7, user_id=OTHER_ID)
    match = FakeHistoryMessage(id=11, raw_text=text + f" @{USERNAME}", entities=[entity])
    client = _client(_card(10, OBSERVED_CARD), match)

    assert (await _texts(client, USERNAME)).match_name is None


async def test_a_username_match_has_no_match_name() -> None:
    client = _client(_card(10), _match(11))

    assert (await _texts(client)).match_name is None


def test_positions_group_consecutive_messages_of_one_group_only() -> None:
    newest_first = [
        FakeHistoryMessage(id=9, grouped_id=2),
        FakeHistoryMessage(id=8, grouped_id=2),
        FakeHistoryMessage(id=7, grouped_id=None),
        FakeHistoryMessage(id=6, grouped_id=None),
        FakeHistoryMessage(id=5, grouped_id=2),
    ]

    positions = group_positions(newest_first)

    assert [[m.id for m in position] for position in positions] == [[8, 9], [7], [6], [5]]


def test_positions_of_nothing_are_nothing() -> None:
    assert group_positions([]) == []


async def test_a_card_photo_download_that_returns_nothing_reports_false(tmp_path: Path) -> None:
    photo = _photo(10, None, OBSERVED_CARD)
    photo.media_bytes = None
    client = _client(photo, _observed_match(11))

    lookup = await _texts(client, None)
    assert lookup.card_photos is not None

    assert await lookup.card_photos.download(0, tmp_path / "card.part.jpg") is False


async def test_a_flood_wait_during_the_download_becomes_a_source_error(tmp_path: Path) -> None:
    failing = FakeClient(read_error=FloodWaitError(request=None, capture=3600))
    photos = TelethonCardPhotos(failing, [_photo(10, None, OBSERVED_CARD)])

    async with asyncio.timeout(1):
        with pytest.raises(ProfileSourceError, match="FloodWaitError"):
            await photos.download(0, tmp_path / "card.part.jpg")


async def test_the_download_uses_only_read_methods(tmp_path: Path) -> None:
    client = _client(*_album(10, (OBSERVED_CARD, "", "")), _observed_match(13))

    lookup = await _texts(client, None)
    await _download_all(lookup, tmp_path)

    assert client.write_attempts == []
    assert set(client.calls) <= READ_METHODS
