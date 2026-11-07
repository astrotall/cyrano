from __future__ import annotations

from cyrano.domain.style import Persona
from cyrano.services.repositories import PersonaRepository


class PersonaService:
    def __init__(self, repository: PersonaRepository) -> None:
        self._repository = repository

    async def get(self) -> Persona | None:
        return await self._repository.get()
