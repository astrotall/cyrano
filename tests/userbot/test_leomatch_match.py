from __future__ import annotations

import pytest

from cyrano.userbot.leomatch_match import (
    ChatMessage,
    MatchEvidence,
    looks_like_match_message,
    match_evidence,
    normalize_username,
)

TG_ID = 5001
OTHER_ID = 6002
USERNAME = "fake_ann"


def _message(
    text: str = "",
    *,
    ids: tuple[int, ...] = (),
    links: tuple[str, ...] = (),
    out: bool = False,
) -> ChatMessage:
    return ChatMessage(id=1, out=out, text=text, mentioned_user_ids=ids, links=links)


def test_a_mention_entity_with_the_contact_id_matches() -> None:
    message = _message("It is a match with Ann", ids=(TG_ID,))

    assert match_evidence(message, TG_ID, USERNAME) is MatchEvidence.TELEGRAM_ID


def test_a_text_url_to_the_user_id_matches() -> None:
    message = _message("It is a match", links=(f"tg://user?id={TG_ID}",))

    assert match_evidence(message, TG_ID, None) is MatchEvidence.TELEGRAM_ID


def test_a_user_link_written_in_the_text_matches() -> None:
    message = _message(f"open tg://user?id={TG_ID} now")

    assert match_evidence(message, TG_ID, None) is MatchEvidence.TELEGRAM_ID


@pytest.mark.parametrize(
    "link",
    [
        f"t.me/{USERNAME}",
        f"https://t.me/{USERNAME}",
        f"http://www.t.me/{USERNAME}",
        f"https://telegram.me/{USERNAME}",
        f"https://t.me/{USERNAME}?start=1",
        f"https://t.me/{USERNAME}/",
        "HTTPS://T.ME/FAKE_ANN",
    ],
)
def test_a_t_me_link_to_the_username_matches(link: str) -> None:
    assert match_evidence(_message(links=(link,)), TG_ID, USERNAME) is MatchEvidence.USERNAME
    assert match_evidence(_message(f"see {link} ok"), TG_ID, USERNAME) is MatchEvidence.USERNAME


@pytest.mark.parametrize(
    "text",
    [
        f"@{USERNAME}",
        f"match with @{USERNAME}",
        f"match with @{USERNAME}, say hi",
        f"match with (@{USERNAME}).",
        f"match\n@{USERNAME}\nnow",
        "@FAKE_ANN",
        "@Fake_Ann is here",
    ],
)
def test_a_standalone_username_matches_case_insensitively(text: str) -> None:
    assert match_evidence(_message(text), TG_ID, USERNAME) is MatchEvidence.USERNAME


def test_a_username_with_an_underscore_matches_exactly() -> None:
    assert match_evidence(_message("@ann_k"), TG_ID, "ann_k") is MatchEvidence.USERNAME
    assert match_evidence(_message("@ann_k_x"), TG_ID, "ann_k") is None


@pytest.mark.parametrize(
    "text",
    [
        "joanna",
        "@joanna",
        "hello joanna",
        "@ann_k",
        "@ann2",
        "@annabel",
        "ann",
        "ann ann",
        "t.me/ann_k",
        "t.me/ann2",
        "t.me/joanna",
        "t.me/ann/123",
        "https://t.me/s/ann",
        "x@ann",
        "mail@ann.example is not a mention of ann",
        "@ann\N{CYRILLIC SMALL LETTER A}",
        "",
    ],
)
def test_false_positives_are_rejected(text: str) -> None:
    assert match_evidence(_message(text), TG_ID, "ann") is None
    assert match_evidence(_message(links=(text,)), TG_ID, "ann") is None


def test_the_boundary_check_works_at_the_start_and_the_end_of_the_text() -> None:
    assert match_evidence(_message("@ann"), TG_ID, "ann") is MatchEvidence.USERNAME
    assert match_evidence(_message("hi @ann"), TG_ID, "ann") is MatchEvidence.USERNAME
    assert match_evidence(_message("@ann hi"), TG_ID, "ann") is MatchEvidence.USERNAME


def test_the_id_evidence_beats_the_username_evidence() -> None:
    message = _message("match with @someone_else", ids=(TG_ID,))

    assert match_evidence(message, TG_ID, USERNAME) is MatchEvidence.TELEGRAM_ID


def test_evidence_by_id_wins_even_when_both_kinds_are_present() -> None:
    message = _message(f"@{USERNAME}", ids=(TG_ID,))

    assert match_evidence(message, TG_ID, USERNAME) is MatchEvidence.TELEGRAM_ID


def test_a_mention_of_another_user_id_is_not_rescued_by_a_matching_username() -> None:
    message = _message(f"match with @{USERNAME}", ids=(OTHER_ID,))

    assert match_evidence(message, TG_ID, USERNAME) is None


def test_another_users_link_is_not_rescued_by_a_matching_username() -> None:
    message = _message(f"@{USERNAME}", links=(f"tg://user?id={OTHER_ID}",))

    assert match_evidence(message, TG_ID, USERNAME) is None


def test_the_contact_id_among_several_ids_matches() -> None:
    assert match_evidence(_message(ids=(OTHER_ID, TG_ID)), TG_ID, None) is MatchEvidence.TELEGRAM_ID


@pytest.mark.parametrize("username", [None, "", "   ", "\t", "@", " @ "])
def test_without_a_usable_username_only_the_id_counts(username: str | None) -> None:
    assert match_evidence(_message("@ann t.me/ann ann"), TG_ID, username) is None
    assert match_evidence(_message(ids=(TG_ID,)), TG_ID, username) is MatchEvidence.TELEGRAM_ID


def test_the_owners_own_messages_never_match() -> None:
    assert match_evidence(_message(f"@{USERNAME}", out=True), TG_ID, USERNAME) is None
    assert match_evidence(_message(ids=(TG_ID,), out=True), TG_ID, USERNAME) is None


def test_a_very_long_digit_run_after_the_user_link_is_ignored_without_an_error() -> None:
    message = _message("tg://user?id=" + "9" * 5000)

    assert match_evidence(message, TG_ID, None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ann", "ann"),
        ("@ann", "ann"),
        ("  @ann  ", "ann"),
        ("Ann", "Ann"),
        (None, None),
        ("", None),
        ("   ", None),
        ("@", None),
    ],
)
def test_username_normalization(raw: str | None, expected: str | None) -> None:
    assert normalize_username(raw) == expected


@pytest.mark.parametrize(
    "message",
    [
        _message("someone", ids=(OTHER_ID,)),
        _message("someone", links=("tg://user?id=77",)),
        _message("open tg://user?id=77"),
        _message("see t.me/somebody"),
        _message("see https://t.me/somebody"),
        _message(links=("https://telegram.me/somebody",)),
    ],
)
def test_messages_with_a_user_mention_or_a_t_me_link_look_like_match_messages(
    message: ChatMessage,
) -> None:
    assert looks_like_match_message(message)


@pytest.mark.parametrize(
    "message",
    [
        _message("Fakename, 25, Faketown - about"),
        _message("@someone is a plain mention"),
        _message("tme/nothing and t.mex/y"),
        _message(),
    ],
)
def test_ordinary_text_does_not_look_like_a_match_message(message: ChatMessage) -> None:
    assert not looks_like_match_message(message)
