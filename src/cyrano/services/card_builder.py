"""Assembly of the owner's card from what is stored about a contact."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError

from cyrano.domain.enums import MessageDirection
from cyrano.domain.models import (
    CARD_BIO_LENGTH,
    CARD_LEOMATCH_ABOUT_LENGTH,
    CARD_LEOMATCH_CITY_LENGTH,
    CARD_LEOMATCH_NAME_LENGTH,
    CARD_LEOMATCH_RAW_LENGTH,
    CARD_MESSAGE_LENGTH,
    CARD_MODE_NAME_LENGTH,
    CARD_NAME_LENGTH,
    MAX_USERNAME_LENGTH,
    CardLeomatch,
    Contact,
    ContactCard,
    ContactProfile,
    LeomatchProfile,
    Message,
    Mode,
    ModeChoice,
)
from cyrano.services.sanitize import clean_text


def profile_of(contact: Contact) -> ContactProfile:
    try:
        return ContactProfile.model_validate(contact.profile)
    except ValidationError:
        return ContactProfile()


def _card_leomatch(leomatch: LeomatchProfile | None) -> CardLeomatch | None:
    if leomatch is None:
        return None
    name = clean_text(leomatch.name, CARD_LEOMATCH_NAME_LENGTH)
    return CardLeomatch(
        name=name,
        name_source=leomatch.name_source if name is not None else None,
        age=leomatch.age,
        city=clean_text(leomatch.city, CARD_LEOMATCH_CITY_LENGTH),
        about=clean_text(leomatch.about, CARD_LEOMATCH_ABOUT_LENGTH),
        raw_text=clean_text(leomatch.raw_text, CARD_LEOMATCH_RAW_LENGTH),
    )


def _message_texts(messages: Sequence[Message]) -> list[str]:
    texts = (
        clean_text(message.text, CARD_MESSAGE_LENGTH)
        for message in messages
        if message.direction is MessageDirection.INCOMING
    )
    return [text for text in texts if text is not None]


def _mode_choices(modes: Sequence[Mode]) -> list[ModeChoice]:
    choices: list[ModeChoice] = []
    for mode in modes:
        name = clean_text(mode.name, CARD_MODE_NAME_LENGTH)
        if mode.enabled and name is not None:
            choices.append(ModeChoice(id=mode.id, name=name))
    return choices


def build_card(contact: Contact, messages: Sequence[Message], modes: Sequence[Mode]) -> ContactCard:
    profile = profile_of(contact)
    leomatch = profile.leomatch
    return ContactCard(
        contact_id=contact.id,
        display_name=clean_text(contact.display_name or profile.display_name, CARD_NAME_LENGTH),
        username=clean_text(contact.username or profile.username, MAX_USERNAME_LENGTH),
        bio=clean_text(profile.bio, CARD_BIO_LENGTH),
        leomatch=_card_leomatch(leomatch),
        messages=_message_texts(messages),
        profile_photo=profile.photo,
        card_photos=list(leomatch.photos) if leomatch is not None else [],
        profile_collected=profile.collected_at is not None,
        modes=_mode_choices(modes),
    )
