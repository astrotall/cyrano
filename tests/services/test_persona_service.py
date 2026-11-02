from __future__ import annotations

from cyrano.db.repositories import SqlPersonaRepository
from cyrano.services.persona_service import PersonaService


async def test_there_is_no_persona_before_an_import(personas: SqlPersonaRepository) -> None:
    assert await PersonaService(personas).get() is None


async def test_the_stored_persona_is_returned(personas: SqlPersonaRepository) -> None:
    stored = await personas.replace("I write short messages")

    assert await PersonaService(personas).get() == stored
