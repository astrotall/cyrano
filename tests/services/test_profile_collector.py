from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cyrano.domain.enums import ContactStatus, LeomatchNameSource
from cyrano.domain.models import (
    MAX_BIO_LENGTH,
    MAX_CARD_PHOTOS,
    MAX_LEOMATCH_NAME_LENGTH,
    Contact,
    ContactProfile,
    ContactReadyEvent,
    ContactUpdate,
    LeomatchProfile,
    NewContactEvent,
)
from cyrano.services.contact_events import InMemoryContactEvents
from cyrano.services.profile_collector import (
    COLLECTION_TIMEOUT_SECONDS,
    PROFILE_PHOTO_DIRECTORY,
    ProfileCollector,
)
from cyrano.services.profile_source import CardPhotos, LeomatchLookup, ProfileSourceError
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.services.fakes import (
    FAKE_BIO,
    FAKE_PHOTO,
    FakeCardPhotos,
    FakeLeomatchSource,
    FakeProfileSource,
    fake_telegram_profile,
)
from tests.userbot.fakes import InMemoryContacts

CLOCK_NOW = datetime(2025, 5, 6, 7, 8, 9, tzinfo=UTC)
TG_ID = 7001
OLD_USERNAME = "fake_old"
OLD_NAME = "Oldfirst Oldlast"
CARD_TEXT = "Cardname, 27, Cardtown - fake card about"
MATCH_TEXT = "match message for fake_fresh"
READY_TIMEOUT = 2.0


class _FailingUpdateContacts(InMemoryContacts):
    def __init__(self, failing_ids: frozenset[int]) -> None:
        super().__init__()
        self._failing_ids = failing_ids

    async def update(self, contact_id: int, data: ContactUpdate) -> Contact:
        if contact_id in self._failing_ids:
            raise ConnectionError("fake database failure")
        return await super().update(contact_id, data)


@dataclass
class Rig:
    contacts: InMemoryContacts
    profiles: FakeProfileSource
    leomatch: FakeLeomatchSource
    new_events: InMemoryContactEvents[NewContactEvent]
    ready_events: InMemoryContactEvents[ContactReadyEvent]
    collector: ProfileCollector
    media_dir: Path
    ready: AsyncIterator[ContactReadyEvent]


def _rig(
    tmp_path: Path,
    *,
    profiles: FakeProfileSource | None = None,
    leomatch: FakeLeomatchSource | None = None,
    contacts: InMemoryContacts | None = None,
    timeout: float = COLLECTION_TIMEOUT_SECONDS,
) -> Rig:
    contacts = contacts if contacts is not None else InMemoryContacts()
    profiles = profiles if profiles is not None else FakeProfileSource()
    leomatch = leomatch if leomatch is not None else FakeLeomatchSource()
    new_events = InMemoryContactEvents[NewContactEvent]()
    ready_events = InMemoryContactEvents[ContactReadyEvent]()
    media_dir = tmp_path / "media"
    collector = ProfileCollector(
        contacts,
        profiles,
        leomatch,
        new_events,
        ready_events,
        media_dir,
        clock=lambda: CLOCK_NOW,
        timeout_seconds=timeout,
    )
    return Rig(
        contacts,
        profiles,
        leomatch,
        new_events,
        ready_events,
        collector,
        media_dir,
        ready_events.subscribe(),
    )


@asynccontextmanager
async def _running(rig: Rig) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(rig.collector.run())
    try:
        yield task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _announce(rig: Rig, contact: Contact) -> None:
    await rig.new_events.publish(NewContactEvent(contact_id=contact.id, message_id=1))


async def _next_ready(rig: Rig) -> ContactReadyEvent:
    return await asyncio.wait_for(anext(rig.ready), timeout=READY_TIMEOUT)


async def _new_contact(rig: Rig, tg_id: int = TG_ID, **kwargs: object) -> Contact:
    return await rig.contacts.seed(
        tg_id,
        username=OLD_USERNAME,
        display_name=OLD_NAME,
        **kwargs,  # type: ignore[arg-type]
    )


async def _collect(rig: Rig, contact: Contact) -> ContactProfile:
    async with _running(rig):
        await _announce(rig, contact)
        assert await _next_ready(rig) == ContactReadyEvent(contact_id=contact.id)
    return await _profile(rig, contact)


async def _stored(rig: Rig, contact: Contact) -> Contact:
    stored = await rig.contacts.get_by_id(contact.id)
    assert stored is not None
    return stored


async def _profile(rig: Rig, contact: Contact) -> ContactProfile:
    return ContactProfile.model_validate((await _stored(rig, contact)).profile)


def _lookup(
    card: str | None = CARD_TEXT,
    match: str | None = MATCH_TEXT,
    match_name: str | None = None,
    photos: CardPhotos | None = None,
) -> LeomatchLookup:
    return LeomatchLookup(
        card_text=card, match_text=match, match_name=match_name, card_photos=photos
    )


def test_the_total_budget_per_contact_is_thirty_seconds() -> None:
    assert COLLECTION_TIMEOUT_SECONDS == 30.0


async def test_every_part_succeeds_and_the_card_can_be_shown(tmp_path: Path) -> None:
    rig = _rig(tmp_path, leomatch=FakeLeomatchSource(_lookup()))
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.username == "fake_fresh"
    assert profile.display_name == "Freshfirst Freshlast"
    assert profile.bio == FAKE_BIO
    assert profile.photo == f"{PROFILE_PHOTO_DIRECTORY}/{contact.id}.jpg"
    assert profile.collected_at == CLOCK_NOW
    assert profile.leomatch is not None
    assert profile.leomatch.raw_text == CARD_TEXT
    assert profile.leomatch.match_text == MATCH_TEXT
    assert profile.leomatch.name_source is LeomatchNameSource.CARD
    assert (profile.leomatch.name, profile.leomatch.age, profile.leomatch.city) == (
        "Cardname",
        27,
        "Cardtown",
    )
    assert rig.collector.finished == 1
    assert rig.collector.failed == 0


async def test_the_contact_status_is_not_changed(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    contact = await _new_contact(rig)

    await _collect(rig, contact)

    assert (await _stored(rig, contact)).status is ContactStatus.NEW


async def test_no_bio_and_no_photo_still_finishes(tmp_path: Path) -> None:
    rig = _rig(
        tmp_path,
        profiles=FakeProfileSource(profile=fake_telegram_profile(bio=None), photo=None),
    )
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.bio is None
    assert profile.photo is None
    assert profile.collected_at == CLOCK_NOW
    assert profile.leomatch is None
    assert not list((rig.media_dir / PROFILE_PHOTO_DIRECTORY).glob("*"))


async def test_the_identity_is_refreshed_from_the_fetched_profile(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    contact = await _new_contact(rig)

    await _collect(rig, contact)

    stored = await _stored(rig, contact)
    assert stored.username == "fake_fresh"
    assert stored.display_name == "Freshfirst Freshlast"


async def test_a_removed_username_is_stored_as_missing(tmp_path: Path) -> None:
    rig = _rig(tmp_path, profiles=FakeProfileSource(profile=fake_telegram_profile(username=None)))
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert (await _stored(rig, contact)).username is None
    assert profile.username is None
    assert rig.leomatch.queries == [(TG_ID, None)]


async def test_the_leomatch_lookup_uses_the_refreshed_username(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    contact = await _new_contact(rig)

    await _collect(rig, contact)

    assert rig.leomatch.queries == [(TG_ID, "fake_fresh")]


async def test_fetched_texts_are_sanitized_and_truncated(tmp_path: Path) -> None:
    profile = fake_telegram_profile(
        username="fake\x00_fresh", display_name="  Fresh\x00first  ", bio="b" * (MAX_BIO_LENGTH * 3)
    )
    rig = _rig(tmp_path, profiles=FakeProfileSource(profile=profile))
    contact = await _new_contact(rig)

    stored = await _collect(rig, contact)

    assert stored.username == "fake_fresh"
    assert stored.display_name == "Freshfirst"
    assert stored.bio == "b" * MAX_BIO_LENGTH


async def test_the_photo_path_is_relative_and_named_by_the_internal_id(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    contact = await _new_contact(rig, tg_id=FAKE_TELEGRAM_ID)

    profile = await _collect(rig, contact)

    assert profile.photo is not None
    relative = Path(profile.photo)
    assert not relative.is_absolute()
    assert relative == Path(PROFILE_PHOTO_DIRECTORY) / f"{contact.id}.jpg"
    assert_absent(FAKE_TELEGRAM_ID, profile.photo)
    assert "fake" not in profile.photo
    assert (rig.media_dir / relative).read_bytes() == FAKE_PHOTO
    assert sorted(p.name for p in (rig.media_dir / PROFILE_PHOTO_DIRECTORY).iterdir()) == [
        f"{contact.id}.jpg"
    ]


async def test_a_failed_photo_download_leaves_no_file_and_other_parts_survive(
    tmp_path: Path,
) -> None:
    rig = _rig(
        tmp_path,
        profiles=FakeProfileSource(photo_error=ProfileSourceError("FloodWaitError")),
        leomatch=FakeLeomatchSource(_lookup()),
    )
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.photo is None
    assert profile.bio == FAKE_BIO
    assert profile.collected_at == CLOCK_NOW
    assert profile.leomatch is not None
    assert not list((rig.media_dir / PROFILE_PHOTO_DIRECTORY).glob("*"))


async def test_a_failed_leomatch_lookup_leaves_the_other_parts_intact(tmp_path: Path) -> None:
    rig = _rig(tmp_path, leomatch=FakeLeomatchSource(error=ProfileSourceError("RPCError")))
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.leomatch is None
    assert profile.photo is not None
    assert profile.bio == FAKE_BIO
    assert profile.collected_at == CLOCK_NOW


async def test_a_failed_telegram_profile_leaves_collected_at_empty_but_publishes_ready(
    tmp_path: Path,
) -> None:
    rig = _rig(
        tmp_path,
        profiles=FakeProfileSource(fetch_error=ProfileSourceError("FloodWaitError")),
        leomatch=FakeLeomatchSource(_lookup()),
    )
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.collected_at is None
    assert profile.photo is not None
    assert profile.leomatch is not None
    assert profile.username == OLD_USERNAME
    stored = await _stored(rig, contact)
    assert (stored.username, stored.display_name) == (OLD_USERNAME, OLD_NAME)
    assert rig.leomatch.queries == [(TG_ID, OLD_USERNAME)]


async def test_ready_is_published_when_every_part_fails(tmp_path: Path) -> None:
    rig = _rig(
        tmp_path,
        profiles=FakeProfileSource(
            fetch_error=ProfileSourceError("A"), photo_error=ProfileSourceError("B")
        ),
        leomatch=FakeLeomatchSource(error=RuntimeError("C")),
    )
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.collected_at is None
    assert profile.photo is None
    assert profile.leomatch is None
    assert rig.collector.failed == 0


async def test_a_telegram_part_that_never_finishes_times_out_and_still_publishes_ready(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path, profiles=FakeProfileSource(fetch_delay=30), timeout=0.05)
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.collected_at is None
    assert rig.profiles.active == 0


async def test_a_late_part_times_out_but_what_was_gathered_is_kept(tmp_path: Path) -> None:
    rig = _rig(tmp_path, profiles=FakeProfileSource(photo_delay=30), timeout=0.05)
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.collected_at == CLOCK_NOW
    assert profile.bio == FAKE_BIO
    assert profile.photo is None
    assert rig.leomatch.queries == []
    assert not list((rig.media_dir / PROFILE_PHOTO_DIRECTORY).glob("*"))


async def test_an_already_collected_contact_is_skipped(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    done = await _new_contact(
        rig, tg_id=1, profile=ContactProfile(collected_at=CLOCK_NOW).model_dump(mode="json")
    )
    other = await rig.contacts.seed(2)

    async with _running(rig):
        await _announce(rig, done)
        await _announce(rig, other)
        assert await _next_ready(rig) == ContactReadyEvent(contact_id=other.id)

    assert rig.profiles.fetched == [2]
    assert rig.ready_events.published == 1


async def test_a_second_event_for_the_same_contact_collects_nothing_twice(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    contact = await _new_contact(rig)
    other = await rig.contacts.seed(2)

    async with _running(rig):
        await _announce(rig, contact)
        await _announce(rig, contact)
        await _announce(rig, other)
        await _next_ready(rig)
        await _next_ready(rig)

    assert rig.profiles.fetched == [TG_ID, 2]


async def test_an_event_for_a_missing_contact_publishes_nothing_and_the_loop_goes_on(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    contact = await _new_contact(rig)

    async with _running(rig):
        await rig.new_events.publish(NewContactEvent(contact_id=424242, message_id=1))
        await _announce(rig, contact)
        assert await _next_ready(rig) == ContactReadyEvent(contact_id=contact.id)

    assert rig.ready_events.published == 1


async def test_events_are_handled_strictly_one_at_a_time_in_order(tmp_path: Path) -> None:
    rig = _rig(tmp_path, profiles=FakeProfileSource(fetch_delay=0.02, photo_delay=0.02))
    contacts = [await rig.contacts.seed(tg_id) for tg_id in (11, 12, 13)]

    async with _running(rig):
        for contact in contacts:
            await _announce(rig, contact)
        ready = [await _next_ready(rig) for _ in contacts]

    assert ready == [ContactReadyEvent(contact_id=c.id) for c in contacts]
    assert rig.profiles.fetched == [11, 12, 13]
    assert rig.profiles.max_active == 1


async def test_an_error_for_one_event_does_not_stop_the_loop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    contacts = _FailingUpdateContacts(failing_ids=frozenset({1}))
    rig = _rig(tmp_path, contacts=contacts)
    broken = await rig.contacts.seed(11)
    healthy = await rig.contacts.seed(12)

    with caplog.at_level(logging.ERROR):
        async with _running(rig) as task:
            await _announce(rig, broken)
            await _announce(rig, healthy)
            assert await _next_ready(rig) == ContactReadyEvent(contact_id=broken.id)
            assert await _next_ready(rig) == ContactReadyEvent(contact_id=healthy.id)
            assert not task.done()

    assert (await _profile(rig, healthy)).collected_at == CLOCK_NOW
    assert rig.collector.failed == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.__dict__["error"] for r in errors] == ["ConnectionError"]
    assert errors[0].__dict__["failed"] == 1


async def test_cancellation_stops_the_loop(tmp_path: Path) -> None:
    rig = _rig(tmp_path)

    task = asyncio.create_task(rig.collector.run())
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_sweep_picks_only_new_contacts_without_collected_at(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    collected = ContactProfile(collected_at=CLOCK_NOW).model_dump(mode="json")
    waiting = await rig.contacts.seed(21)
    broken_profile = await rig.contacts.seed(22, profile={"photo": 5})
    await rig.contacts.seed(23, profile=collected)
    await rig.contacts.seed(24, status=ContactStatus.PENDING)
    await rig.contacts.seed(25, status=ContactStatus.ACTIVE)
    await rig.contacts.seed(26, status=ContactStatus.IGNORED)

    async with _running(rig):
        ready = [await _next_ready(rig), await _next_ready(rig)]

    assert rig.profiles.fetched == [21, 22]
    assert ready == [
        ContactReadyEvent(contact_id=waiting.id),
        ContactReadyEvent(contact_id=broken_profile.id),
    ]
    assert rig.ready_events.published == 2


async def test_a_contact_found_by_the_sweep_is_not_collected_again_by_its_event(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path)
    first = await rig.contacts.seed(31)
    second = await rig.contacts.seed(32)
    await _announce(rig, second)
    await _announce(rig, first)
    late = await rig.contacts.seed(33)

    async with _running(rig):
        ready = [await _next_ready(rig) for _ in range(2)]
        await _announce(rig, late)
        ready.append(await _next_ready(rig))

    assert [event.contact_id for event in ready] == [first.id, second.id, late.id]
    assert rig.profiles.fetched == [31, 32, 33]
    assert rig.ready_events.published == 3


async def test_a_contact_whose_telegram_part_failed_is_retried_by_the_next_sweep(
    tmp_path: Path,
) -> None:
    failing = _rig(tmp_path, profiles=FakeProfileSource(fetch_error=ProfileSourceError("X")))
    contact = await _new_contact(failing)
    assert (await _collect(failing, contact)).collected_at is None

    retry = _rig(tmp_path, contacts=failing.contacts)
    async with _running(retry):
        await _next_ready(retry)

    assert (await _profile(retry, contact)).collected_at == CLOCK_NOW
    assert (await _stored(retry, contact)).username == "fake_fresh"


async def test_a_failing_sweep_does_not_stop_the_event_loop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class _BrokenListing(InMemoryContacts):
        async def list_by_status(self, status: ContactStatus) -> list[Contact]:
            raise ConnectionError("fake database failure")

    rig = _rig(tmp_path, contacts=_BrokenListing())
    contact = await _new_contact(rig)

    with caplog.at_level(logging.ERROR):
        assert (await _collect(rig, contact)).collected_at == CLOCK_NOW

    assert [r.__dict__["error"] for r in caplog.records if r.levelno == logging.ERROR] == [
        "ConnectionError"
    ]


async def test_an_earlier_photo_survives_a_failed_retry(tmp_path: Path) -> None:
    rig = _rig(tmp_path, profiles=FakeProfileSource(photo_error=ProfileSourceError("X")))
    earlier = ContactProfile(photo=f"{PROFILE_PHOTO_DIRECTORY}/1.jpg").model_dump(mode="json")
    contact = await _new_contact(rig, profile=earlier)

    profile = await _collect(rig, contact)

    assert profile.photo == f"{PROFILE_PHOTO_DIRECTORY}/1.jpg"
    assert profile.collected_at == CLOCK_NOW


async def test_leomatch_without_a_card_keeps_the_match_text_apart_from_the_card_text(
    tmp_path: Path,
) -> None:
    rig = _rig(tmp_path, leomatch=FakeLeomatchSource(_lookup(card=None)))
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.raw_text is None
    assert profile.leomatch.match_text == MATCH_TEXT
    assert profile.leomatch.name is None
    assert profile.leomatch.name_source is None


async def test_without_a_card_the_name_comes_from_the_match_link(tmp_path: Path) -> None:
    lookup = _lookup(card=None, match_name="Linkname")
    rig = _rig(tmp_path, leomatch=FakeLeomatchSource(lookup))
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.raw_text is None
    assert (profile.leomatch.name, profile.leomatch.name_source) == (
        "Linkname",
        LeomatchNameSource.MATCH,
    )
    assert (profile.leomatch.age, profile.leomatch.city, profile.leomatch.about) == (None,) * 3


async def test_a_card_that_does_not_parse_also_takes_the_name_from_the_match_link(
    tmp_path: Path,
) -> None:
    lookup = _lookup(card="free card text", match_name="Linkname")
    rig = _rig(tmp_path, leomatch=FakeLeomatchSource(lookup))
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.raw_text == "free card text"
    assert (profile.leomatch.name, profile.leomatch.name_source) == (
        "Linkname",
        LeomatchNameSource.MATCH,
    )


async def test_a_parsed_card_name_wins_over_the_match_link_name(tmp_path: Path) -> None:
    lookup = _lookup(match_name="Linkname")
    rig = _rig(tmp_path, leomatch=FakeLeomatchSource(lookup))
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert (profile.leomatch.name, profile.leomatch.name_source) == (
        "Cardname",
        LeomatchNameSource.CARD,
    )


async def test_the_match_link_name_is_sanitized_and_truncated(tmp_path: Path) -> None:
    lookup = _lookup(card=None, match_name="  Nul\x00name" + "x" * 500)
    rig = _rig(tmp_path, leomatch=FakeLeomatchSource(lookup))
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.name is not None
    assert profile.leomatch.name.startswith("Nulname")
    assert len(profile.leomatch.name) == MAX_LEOMATCH_NAME_LENGTH


async def test_leomatch_text_that_does_not_parse_keeps_only_the_raw_text(tmp_path: Path) -> None:
    rig = _rig(tmp_path, leomatch=FakeLeomatchSource(_lookup(card="free card text")))
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.raw_text == "free card text"
    assert profile.leomatch.match_text == MATCH_TEXT
    assert profile.leomatch.age is None


async def test_nothing_personal_reaches_the_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret_bio = "bio-that-must-not-leak"
    secret_card = "Leakname, 29, Leaktown - card-that-must-not-leak"
    rigs = [
        _rig(
            tmp_path / "ok",
            profiles=FakeProfileSource(profile=fake_telegram_profile(bio=secret_bio)),
            leomatch=FakeLeomatchSource(_lookup(card=secret_card)),
        ),
        _rig(
            tmp_path / "failing",
            profiles=FakeProfileSource(
                fetch_error=ProfileSourceError("leak-fetch"),
                photo_error=ProfileSourceError("leak-photo"),
            ),
            leomatch=FakeLeomatchSource(error=ProfileSourceError("leak-leomatch")),
        ),
    ]

    with caplog.at_level(logging.DEBUG):
        for rig in rigs:
            contact = await _new_contact(rig, tg_id=FAKE_TELEGRAM_ID)
            await _collect(rig, contact)

    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "profile collection finished" in logged
    for forbidden in (
        secret_bio,
        secret_card,
        "Leakname",
        "card-that-must-not-leak",
        MATCH_TEXT,
        "fake_fresh",
        OLD_USERNAME,
        "Freshfirst",
        OLD_NAME,
        str(FAKE_TELEGRAM_ID),
        ".jpg",
        "profiles/",
        "leak-fetch",
        "leak-photo",
        "leak-leomatch",
    ):
        assert_absent(forbidden, logged)


def _card_rig(
    tmp_path: Path, card_photos: FakeCardPhotos | None, *, profiles: FakeProfileSource | None = None
) -> Rig:
    return _rig(
        tmp_path, profiles=profiles, leomatch=FakeLeomatchSource(_lookup(photos=card_photos))
    )


def _profile_files(rig: Rig) -> list[str]:
    return sorted(p.name for p in (rig.media_dir / PROFILE_PHOTO_DIRECTORY).iterdir())


def _card_path(contact: Contact, number: int) -> str:
    return f"{PROFILE_PHOTO_DIRECTORY}/{contact.id}-card-{number}.jpg"


async def test_all_card_photos_are_stored_under_the_internal_id(tmp_path: Path) -> None:
    card_photos = FakeCardPhotos(3, contents={0: b"one", 1: b"two", 2: b"three"})
    rig = _card_rig(tmp_path, card_photos)
    contact = await _new_contact(rig, tg_id=FAKE_TELEGRAM_ID)

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.photos == [_card_path(contact, n) for n in (1, 2, 3)]
    for stored in profile.leomatch.photos:
        assert not Path(stored).is_absolute()
        assert_absent(FAKE_TELEGRAM_ID, stored)
    contents = [(rig.media_dir / stored).read_bytes() for stored in profile.leomatch.photos]
    assert contents == [b"one", b"two", b"three"]
    assert profile.photo == f"{PROFILE_PHOTO_DIRECTORY}/{contact.id}.jpg"
    assert (rig.media_dir / str(profile.photo)).read_bytes() == FAKE_PHOTO
    assert _profile_files(rig) == [
        f"{contact.id}-card-1.jpg",
        f"{contact.id}-card-2.jpg",
        f"{contact.id}-card-3.jpg",
        f"{contact.id}.jpg",
    ]


async def test_card_photos_are_downloaded_in_order_through_part_files(tmp_path: Path) -> None:
    card_photos = FakeCardPhotos(2)
    rig = _card_rig(tmp_path, card_photos)
    contact = await _new_contact(rig)

    await _collect(rig, contact)

    directory = rig.media_dir / PROFILE_PHOTO_DIRECTORY
    assert card_photos.downloads == [
        (0, directory / f"{contact.id}-card-1.part.jpg"),
        (1, directory / f"{contact.id}-card-2.part.jpg"),
    ]
    assert not list(directory.glob("*.part*"))


async def test_no_more_than_the_photo_limit_is_downloaded(tmp_path: Path) -> None:
    card_photos = FakeCardPhotos(MAX_CARD_PHOTOS + 3)
    rig = _card_rig(tmp_path, card_photos)
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert len(profile.leomatch.photos) == MAX_CARD_PHOTOS
    assert [index for index, _ in card_photos.downloads] == list(range(MAX_CARD_PHOTOS))


async def test_a_card_without_photos_stores_none(tmp_path: Path) -> None:
    rig = _card_rig(tmp_path, None)
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.photos == []
    assert profile.photo is not None
    assert _profile_files(rig) == [f"{contact.id}.jpg"]


async def test_photos_that_download_nothing_store_no_path(tmp_path: Path) -> None:
    rig = _card_rig(tmp_path, FakeCardPhotos(2, contents={0: None, 1: None}))
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.photos == []


async def test_one_failing_photo_does_not_block_the_others(tmp_path: Path) -> None:
    card_photos = FakeCardPhotos(3, errors={1: ProfileSourceError("FloodWaitError")})
    rig = _card_rig(tmp_path, card_photos)
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.photos == [_card_path(contact, 1), _card_path(contact, 3)]
    assert profile.leomatch.raw_text == CARD_TEXT
    assert profile.leomatch.match_text == MATCH_TEXT
    assert profile.photo == f"{PROFILE_PHOTO_DIRECTORY}/{contact.id}.jpg"
    assert profile.bio == FAKE_BIO
    assert profile.collected_at == CLOCK_NOW
    assert _profile_files(rig) == [
        f"{contact.id}-card-1.jpg",
        f"{contact.id}-card-3.jpg",
        f"{contact.id}.jpg",
    ]


async def test_every_photo_failing_leaves_no_file_and_the_other_parts_survive(
    tmp_path: Path,
) -> None:
    errors = {0: ProfileSourceError("FloodWaitError"), 1: RuntimeError("unexpected")}
    rig = _card_rig(tmp_path, FakeCardPhotos(2, errors=errors))
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.photos == []
    assert profile.leomatch.raw_text == CARD_TEXT
    assert profile.collected_at == CLOCK_NOW
    assert _profile_files(rig) == [f"{contact.id}.jpg"]
    assert rig.collector.failed == 0


async def test_a_failed_profile_photo_does_not_touch_the_card_photos(tmp_path: Path) -> None:
    rig = _card_rig(
        tmp_path, FakeCardPhotos(2), profiles=FakeProfileSource(photo_error=ProfileSourceError("X"))
    )
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.photo is None
    assert profile.leomatch is not None
    assert profile.leomatch.photos == [_card_path(contact, 1), _card_path(contact, 2)]
    assert _profile_files(rig) == [f"{contact.id}-card-1.jpg", f"{contact.id}-card-2.jpg"]


async def test_a_failed_leomatch_lookup_downloads_no_card_photo(tmp_path: Path) -> None:
    card_photos = FakeCardPhotos(2)
    rig = _rig(
        tmp_path,
        leomatch=FakeLeomatchSource(
            _lookup(photos=card_photos), error=ProfileSourceError("RPCError")
        ),
    )
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.leomatch is None
    assert card_photos.downloads == []


async def test_card_photos_cut_off_by_the_budget_leave_no_part_file(tmp_path: Path) -> None:
    rig = _rig(
        tmp_path,
        leomatch=FakeLeomatchSource(_lookup(photos=FakeCardPhotos(2, delay=5.0))),
        timeout=0.2,
    )
    contact = await _new_contact(rig)

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.photos == []
    assert profile.photo == f"{PROFILE_PHOTO_DIRECTORY}/{contact.id}.jpg"
    assert _profile_files(rig) == [f"{contact.id}.jpg"]


def _earlier_leomatch(*photos: str) -> dict[str, object]:
    earlier = ContactProfile(
        leomatch=LeomatchProfile(raw_text="earlier card text", photos=list(photos))
    )
    return earlier.model_dump(mode="json")


async def test_earlier_paths_survive_failed_downloads_and_the_list_never_shrinks(
    tmp_path: Path,
) -> None:
    earlier = [_old_path(n) for n in (1, 2, 3)]
    errors: dict[int, Exception] = {index: ProfileSourceError("X") for index in range(3)}
    rig = _card_rig(tmp_path, FakeCardPhotos(3, errors=errors))
    contact = await _new_contact(rig, profile=_earlier_leomatch(*earlier))

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.raw_text == CARD_TEXT
    assert profile.leomatch.photos == earlier


async def test_a_retry_with_fewer_photos_keeps_the_longer_stored_list(tmp_path: Path) -> None:
    earlier = [_old_path(n) for n in (1, 2, 3)]
    rig = _card_rig(tmp_path, FakeCardPhotos(1))
    contact = await _new_contact(rig, profile=_earlier_leomatch(*earlier))

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.photos == [_card_path(contact, 1), *earlier[1:]]


async def test_a_failure_at_one_index_keeps_the_old_path_there(tmp_path: Path) -> None:
    earlier = [_old_path(1), _old_path(2)]
    rig = _card_rig(tmp_path, FakeCardPhotos(3, errors={0: ProfileSourceError("X")}))
    contact = await _new_contact(rig, profile=_earlier_leomatch(*earlier))

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.photos == [
        earlier[0],
        _card_path(contact, 2),
        _card_path(contact, 3),
    ]


async def test_earlier_photos_survive_a_retry_whose_card_has_no_photos(tmp_path: Path) -> None:
    earlier = [_old_path(1)]
    rig = _card_rig(tmp_path, None)
    contact = await _new_contact(rig, profile=_earlier_leomatch(*earlier))

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.photos == earlier


async def test_an_earlier_leomatch_with_its_photos_survives_a_failed_lookup(
    tmp_path: Path,
) -> None:
    earlier = [_old_path(1)]
    rig = _rig(tmp_path, leomatch=FakeLeomatchSource(error=ProfileSourceError("RPCError")))
    contact = await _new_contact(rig, profile=_earlier_leomatch(*earlier))

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.raw_text == "earlier card text"
    assert profile.leomatch.photos == earlier


async def test_a_legacy_single_card_photo_is_kept_and_then_replaced(tmp_path: Path) -> None:
    legacy = {"leomatch": {"raw_text": "earlier card text", "photo": "profiles/old-card.jpg"}}
    rig = _card_rig(tmp_path, FakeCardPhotos(2))
    contact = await _new_contact(rig, profile=legacy)

    profile = await _collect(rig, contact)

    assert profile.leomatch is not None
    assert profile.leomatch.photos == [_card_path(contact, 1), _card_path(contact, 2)]


async def test_nothing_about_the_card_photos_reaches_the_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rigs = [
        _card_rig(tmp_path / "ok", FakeCardPhotos(2)),
        _card_rig(
            tmp_path / "failing",
            FakeCardPhotos(2, errors={0: ProfileSourceError("leak-card")}),
        ),
    ]

    with caplog.at_level(logging.DEBUG):
        for rig in rigs:
            contact = await _new_contact(rig, tg_id=FAKE_TELEGRAM_ID)
            await _collect(rig, contact)

    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "profile collection finished" in logged
    for forbidden in ("-card", ".jpg", "profiles/", "leak-card", str(FAKE_TELEGRAM_ID), CARD_TEXT):
        assert_absent(forbidden, logged)


def _old_path(number: int) -> str:
    return f"{PROFILE_PHOTO_DIRECTORY}/old-card-{number}.jpg"
