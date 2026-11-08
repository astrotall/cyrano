from __future__ import annotations

from cyrano.domain.enums import ContactGender, GenderSource
from cyrano.domain.gender import UNRESOLVED_GENDER, ResolvedGender
from cyrano.domain.models import Contact
from cyrano.domain.summary import read_stored_summary
from cyrano.services.card_builder import profile_of
from cyrano.services.gender_guess import guess_gender


def contact_name_for_guess(contact: Contact) -> str | None:
    profile = profile_of(contact)
    leomatch = profile.leomatch
    card_name = None if leomatch is None else leomatch.name
    return card_name or contact.display_name or profile.display_name


class GenderResolver:
    def __init__(self, default: ContactGender | None = None) -> None:
        self._default = default

    def resolve(self, contact: Contact) -> ResolvedGender:
        if contact.gender is not None:
            return ResolvedGender(value=contact.gender, source=GenderSource.OWNER)
        stored = read_stored_summary(contact.summary)
        if stored is not None and stored.data.gender is not None:
            return ResolvedGender(value=stored.data.gender, source=GenderSource.SUMMARY)
        guessed = guess_gender(contact_name_for_guess(contact))
        if guessed is not None:
            return ResolvedGender(value=guessed, source=GenderSource.NAME)
        if self._default is not None:
            return ResolvedGender(value=self._default, source=GenderSource.DEFAULT)
        return UNRESOLVED_GENDER
