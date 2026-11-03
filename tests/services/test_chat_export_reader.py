from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cyrano.domain.chat_export import MAX_EXPORT_BYTES, SkipReason
from cyrano.domain.errors import ChatExportError
from cyrano.services import chat_export_reader
from cyrano.services.chat_export_reader import (
    ExportData,
    load_export_file,
    message_items,
    read_messages,
)
from tests.chat_export_data import (
    BACKTICK,
    OTHER,
    OWNER,
    OWNER_ID,
    at,
    export_document,
    message,
    sample_shaped_message,
)


def _read(*items: object) -> ExportData:
    return read_messages(list(items))


def test_text_as_a_string_is_kept() -> None:
    data = _read(message(0, OWNER, "  plain text  "))

    assert [item.text for item in data.messages] == ["plain text"]


def test_text_as_a_list_with_entity_objects_is_flattened() -> None:
    text = [
        "see ",
        {"type": "link", "text": "https://example.org/a"},
        " and ",
        {"type": "bold", "text": "this"},
        {"type": "weird", "other": 1},
        7,
        None,
        ["nested"],
    ]

    data = _read(message(0, OWNER, text))

    assert [item.text for item in data.messages] == ["see https://example.org/a and this"]


def test_a_null_sender_is_skipped_and_counted() -> None:
    data = _read(message(0, None, "hello"), message(1, "   ", "hello"), message(2, 7, "hello"))

    assert data.messages == ()
    assert data.skipped == {SkipReason.NO_SENDER: 3}
    assert data.senders == frozenset()


def test_missing_fields_never_crash() -> None:
    data = _read(
        {},
        {"type": "message"},
        {"type": "message", "from": OWNER},
        {"type": "message", "from": OWNER, "text": "x"},
        "text",
        5,
        None,
        [1],
    )

    assert data.read == 8
    assert data.messages == ()
    assert data.skipped == {
        SkipReason.NOT_A_MESSAGE: 1,
        SkipReason.EMPTY_TEXT: 2,
        SkipReason.NO_TIME: 1,
        SkipReason.UNREADABLE: 4,
    }


def test_service_messages_are_skipped() -> None:
    data = _read(
        message(0, OWNER, "x", type="service"),
        {
            "id": 5,
            "type": "service",
            "actor": OTHER,
            "action": "phone_call",
            "date": "2024-03-01T10:00:00",
        },
    )

    assert data.messages == ()
    assert data.skipped == {SkipReason.NOT_A_MESSAGE: 2}


def test_forwarded_messages_are_skipped_even_with_a_null_origin() -> None:
    data = _read(
        message(0, OWNER, "from elsewhere", forwarded_from="Someone"),
        message(1, OWNER, "hidden origin", forwarded_from=None),
        message(2, OWNER, "mine"),
    )

    assert [item.text for item in data.messages] == ["mine"]
    assert data.skipped == {SkipReason.FORWARDED: 2}


def test_stickers_voice_and_media_without_caption_are_skipped() -> None:
    data = _read(
        message(0, OWNER, "", media_type="sticker", sticker_emoji="x"),
        message(1, OWNER, "", media_type="voice_message", file="voice.ogg"),
        message(2, OWNER, [], media_type="video_message"),
        message(3, OWNER, "   ", photo="photo.jpg"),
    )

    assert data.messages == ()
    assert data.skipped == {SkipReason.EMPTY_TEXT: 4}


def test_a_captioned_media_message_is_kept_by_its_caption() -> None:
    data = _read(message(0, OWNER, "the caption", photo="photo.jpg", media_type="video_file"))

    assert [item.text for item in data.messages] == ["the caption"]


def test_the_unix_time_is_preferred_over_the_date() -> None:
    item = message(0, OWNER, "x", date="2001-01-01T00:00:00")

    assert _read(item).messages[0].time == at(0)


def test_the_date_is_used_when_the_unix_time_is_missing_or_invalid() -> None:
    first = message(0, OWNER, "a", date="2024-03-01T10:00:00")
    first.pop("date_unixtime")
    second = message(0, OWNER, "b", date="2024-03-01T10:05:00", date_unixtime="not a number")
    third = message(0, OWNER, "c", date="2024-03-01T10:10:00", date_unixtime=True)
    huge = message(0, OWNER, "d", date="2024-03-01T10:15:00", date_unixtime="9" * 30)

    times = [item.time for item in _read(first, second, third, huge).messages]

    assert times == [at(0), at(5), at(10), at(15)]


def test_a_date_with_an_offset_is_converted_to_utc() -> None:
    item = message(0, OWNER, "x", date="2024-03-01T13:00:00+03:00")
    item.pop("date_unixtime")

    assert _read(item).messages[0].time == datetime(2024, 3, 1, 10, 0, tzinfo=UTC)


def test_a_message_without_a_usable_time_is_skipped_and_counted() -> None:
    first = message(0, OWNER, "x", date="garbage", date_unixtime="")
    second = message(0, OWNER, "y")
    second.pop("date")
    second.pop("date_unixtime")

    data = _read(first, second)

    assert data.messages == ()
    assert data.skipped == {SkipReason.NO_TIME: 2}


def test_messages_are_ordered_by_time_and_equal_times_keep_their_order() -> None:
    data = _read(
        message(10, OWNER, "late"),
        message(5, OTHER, "first of two"),
        message(5, OWNER, "second of two"),
        message(0, OTHER, "earliest"),
    )

    assert [item.text for item in data.messages] == [
        "earliest",
        "first of two",
        "second of two",
        "late",
    ]


def test_unknown_extra_fields_are_ignored() -> None:
    item = message(0, OWNER, "x", future_field={"a": [1, 2, 3]}, reactions=[{"emoji": "y"}])

    data = _read(item)

    assert len(data.messages) == 1
    assert data.skipped == {}


def test_senders_include_every_name_seen_even_on_skipped_messages() -> None:
    data = _read(
        message(0, OWNER, "x"),
        message(1, OTHER, "", media_type="sticker"),
        message(2, "Third Person", "x", forwarded_from="A"),
    )

    assert data.senders == {OWNER, OTHER, "Third Person"}


def test_a_nul_byte_is_removed_from_the_text() -> None:
    data = _read(message(0, OWNER, "a\x00b"), message(1, OWNER, "\x00"))

    assert [item.text for item in data.messages] == ["ab"]
    assert data.skipped == {SkipReason.EMPTY_TEXT: 1}


def test_an_account_wide_export_is_refused() -> None:
    with pytest.raises(ChatExportError, match="account-wide export; export one chat"):
        message_items({"about": "x", "chats": {"list": []}})


@pytest.mark.parametrize("document", [{}, {"messages": "no"}, {"messages": None}, [], "text", 5])
def test_a_json_without_messages_is_refused(document: object) -> None:
    with pytest.raises(ChatExportError, match="no messages"):
        message_items(document)


def test_an_empty_message_list_is_readable() -> None:
    assert message_items({"messages": []}) == []


def test_a_file_is_loaded_with_a_byte_order_mark(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(export_document()).encode())

    assert message_items(load_export_file(path)) == []


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ChatExportError, match="not found"):
        load_export_file(tmp_path / "result.json")


def test_a_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ChatExportError, match="is a directory"):
        load_export_file(tmp_path)


@pytest.mark.parametrize("name", ["messages.html", "MESSAGES.HTM", "chat.Html"])
def test_html_input_is_refused_by_its_name(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    path.write_text("<html></html>", encoding="utf-8")

    with pytest.raises(ChatExportError, match="HTML exports are not supported"):
        load_export_file(path)


def test_html_content_with_a_json_name_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text("  \n<!DOCTYPE html><html></html>", encoding="utf-8")

    with pytest.raises(ChatExportError, match="HTML exports are not supported"):
        load_export_file(path)


def test_invalid_json_reports_a_position_and_no_content(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text('{"messages": [secret-text-zq]}', encoding="utf-8")

    with pytest.raises(ChatExportError, match="not valid JSON") as caught:
        load_export_file(path)

    assert "secret-text-zq" not in str(caught.value)
    assert "line 1 column" in str(caught.value)


def test_a_file_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_bytes(b'{"a": "\xff\xfe"}')

    with pytest.raises(ChatExportError, match="not valid UTF-8"):
        load_export_file(path)


def test_deeply_nested_json_is_refused_readably(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text("[" * 200000, encoding="utf-8")

    with pytest.raises(ChatExportError, match="nested too deeply"):
        load_export_file(path)


def test_an_oversized_file_is_refused_before_it_is_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "result.json"
    path.write_text(json.dumps(export_document(message(0, OWNER, "x" * 100))), encoding="utf-8")
    monkeypatch.setattr(chat_export_reader, "MAX_EXPORT_BYTES", 50)

    with pytest.raises(ChatExportError, match="larger than 50 bytes"):
        load_export_file(path)


def test_the_size_cap_is_fifty_mebibytes() -> None:
    assert MAX_EXPORT_BYTES == 50 * 1024 * 1024


def test_a_message_shaped_like_a_real_export_is_read() -> None:
    data = _read(sample_shaped_message())

    assert len(data.messages) == 1
    item = data.messages[0]
    assert (item.sender, item.sender_id, item.text) == (BACKTICK, OWNER_ID, "hello")
    assert item.time == datetime.fromtimestamp(1789159381, tz=UTC)
    assert data.senders == {BACKTICK}
    assert data.sender_ids == {OWNER_ID}


def test_a_null_name_with_a_valid_id_is_kept_when_reading_by_id() -> None:
    item = message(0, None, "hello", from_id=OWNER_ID)

    by_id = read_messages([item], by_id=True)
    by_name = read_messages([item])

    assert [(m.sender, m.sender_id) for m in by_id.messages] == [(None, OWNER_ID)]
    assert by_id.skipped == {}
    assert by_name.messages == ()
    assert by_name.skipped == {SkipReason.NO_SENDER: 1}
    assert by_name.sender_ids == {OWNER_ID}


@pytest.mark.parametrize("identifier", [None, "", "  ", 7, ["user1"]])
def test_a_message_without_a_usable_id_is_skipped_when_reading_by_id(identifier: object) -> None:
    data = read_messages([message(0, OWNER, "hello", from_id=identifier)], by_id=True)

    assert data.messages == ()
    assert data.skipped == {SkipReason.NO_SENDER: 1}
    assert data.sender_ids == frozenset()
