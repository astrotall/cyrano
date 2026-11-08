from __future__ import annotations

import logging

from cyrano.domain.errors import ContactNotFound, ModeNameNotFound, PersonaMissing
from cyrano.domain.models import Contact, Mode
from cyrano.domain.reply import BuiltPrompt, GeneratedReply
from cyrano.domain.summary import read_stored_summary
from cyrano.prompts.render import render_summary_layer
from cyrano.services.contact_facts import build_contact_facts
from cyrano.services.llm_client import LLMClient
from cyrano.services.persona_service import PersonaService
from cyrano.services.prompt_builder import PromptBuilder
from cyrano.services.reply_postprocess import clean_reply, contains_needs_owner, is_bare_needs_owner
from cyrano.services.repositories import ContactRepository, MessageRepository, ModeRepository
from cyrano.services.style_examples import ExampleSelector

logger = logging.getLogger(__name__)


class ReplyGenerator:
    def __init__(
        self,
        *,
        contacts: ContactRepository,
        messages: MessageRepository,
        modes: ModeRepository,
        persona: PersonaService,
        examples: ExampleSelector,
        builder: PromptBuilder,
        client: LLMClient,
        history_limit: int,
    ) -> None:
        self._contacts = contacts
        self._messages = messages
        self._modes = modes
        self._persona = persona
        self._examples = examples
        self._builder = builder
        self._client = client
        self._history_limit = history_limit

    async def generate(
        self,
        contact_id: int,
        mode_name: str | None = None,
        *,
        summary: str | None = None,
        max_tokens: int | None = None,
    ) -> GeneratedReply:
        prompt = await self.prepare(contact_id, mode_name, summary=summary)
        return await self.complete(prompt, max_tokens=max_tokens)

    async def prepare(
        self, contact_id: int, mode_name: str | None = None, *, summary: str | None = None
    ) -> BuiltPrompt:
        contact = await self._contacts.get_by_id(contact_id)
        if contact is None:
            raise ContactNotFound(contact_id)
        persona = await self._persona.get()
        if persona is None:
            raise PersonaMissing
        history = await self._messages.list_last(contact_id, self._history_limit)
        mode = await self._resolve_mode(contact.mode_id, mode_name)
        examples = await self._examples.select(mode.name)
        return self._builder.build(
            contact_id=contact_id,
            persona=persona.text,
            mode_fragment=mode.prompt_fragment,
            facts=build_contact_facts(contact),
            summary=summary if summary is not None else _stored_summary_text(contact),
            messages=history,
            examples=examples,
        )

    async def complete(
        self, prompt: BuiltPrompt, *, max_tokens: int | None = None
    ) -> GeneratedReply:
        result = await self._client.complete(prompt.messages, max_tokens=max_tokens)
        needs_owner = contains_needs_owner(result.text)
        text = "" if needs_owner else clean_reply(result.text)
        logger.info(
            "reply generated",
            extra={
                "examples_kept": prompt.examples_kept,
                "messages_kept": prompt.messages_kept,
                "prompt_characters": prompt.characters,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "needs_owner": needs_owner,
                "needs_owner_leaked": needs_owner and not is_bare_needs_owner(result.text),
                "truncated": result.truncated,
            },
        )
        return GeneratedReply(
            text=text,
            usage=result.usage,
            needs_owner=needs_owner,
            truncated=result.truncated,
            examples_offered=prompt.examples_offered,
            examples_kept=prompt.examples_kept,
            messages_offered=prompt.messages_offered,
            messages_kept=prompt.messages_kept,
            prompt_characters=prompt.characters,
        )

    async def _resolve_mode(self, mode_id: int | None, mode_name: str | None) -> Mode:
        if mode_name is None:
            return await self._modes.resolve_by_id(mode_id)
        if await self._modes.get_by_name(mode_name) is None:
            raise ModeNameNotFound
        return await self._modes.resolve_by_name(mode_name)


def _stored_summary_text(contact: Contact) -> str | None:
    stored = read_stored_summary(contact.summary)
    return None if stored is None else render_summary_layer(stored.data)
