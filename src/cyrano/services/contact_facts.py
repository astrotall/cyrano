from __future__ import annotations

from cyrano.domain.models import Contact
from cyrano.domain.reply import (
    FACT_ABOUT_LIMIT,
    FACT_BIO_LIMIT,
    FACT_CITY_LIMIT,
    FACT_NAME_LIMIT,
    ContactFacts,
)
from cyrano.services.card_builder import profile_of
from cyrano.services.sanitize import clean_text


def build_contact_facts(contact: Contact) -> ContactFacts:
    profile = profile_of(contact)
    leomatch = profile.leomatch
    card_name = None if leomatch is None else leomatch.name
    name = (
        clean_text(card_name, FACT_NAME_LIMIT)
        or clean_text(contact.display_name, FACT_NAME_LIMIT)
        or clean_text(profile.display_name, FACT_NAME_LIMIT)
    )
    return ContactFacts(
        name=name,
        age=None if leomatch is None else leomatch.age,
        city=None if leomatch is None else clean_text(leomatch.city, FACT_CITY_LIMIT),
        about=None if leomatch is None else clean_text(leomatch.about, FACT_ABOUT_LIMIT),
        bio=clean_text(profile.bio, FACT_BIO_LIMIT),
    )
