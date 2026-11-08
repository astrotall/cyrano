from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL

from cyrano import main as main_module
from cyrano.config import Settings
from cyrano.db.repositories import SqlSummaryRepository
from cyrano.db.session import SessionFactory
from cyrano.domain.enums import ContactGender
from cyrano.domain.errors import LLMAuthError, LLMUnavailableError
from cyrano.domain.models import ModeCreate
from cyrano.domain.schedule import ScheduleProfile
from cyrano.domain.summary import SUMMARY_VERSION, ContactSummary, StoredSummary, SummaryCursor
from cyrano.llm.factory import LLMClientFactory
from cyrano.main import main
from cyrano.prompts.texts import DIALOGUE_BOUNDARY, SUMMARY_HEADER, SUMMARY_MEMORY_NOTE
from cyrano.reply_cli import (
    NEEDS_OWNER_LINE,
    PERSONAL_DATA_WARNING,
    PreviewClock,
    ReplyPreviewCommand,
    ReplyPreviewRequest,
    reply_preview_request,
)
from cyrano.services.gender import GenderResolver
from cyrano.services.humanization import ScheduleService
from cyrano.services.persona_service import PersonaService
from cyrano.services.prompt_builder import PromptBuilder
from cyrano.services.reply_generator import ReplyGenerator
from cyrano.services.style_examples import ExampleSelector
from tests.db.conftest import execute
from tests.llm.fakes import Script, completion_response, error_response
from tests.privacy import FAKE_TELEGRAM_ID, assert_absent
from tests.reply_data import BIG_BUDGET
from tests.reply_db import (
    BIO_MARKER,
    HISTORY_LIMIT,
    MESSAGE_MARKER,
    PERSONA_MARKER,
    REPLY_MARKER,
    USERNAME_MARKER,
    Repositories,
    ready_contact,
    row_counts,
)
from tests.services.fakes import ScriptedLLMClient
from tests.services.outbox_fakes import InMemoryScheduleProfiles

MODEL = "fake-model"
GENDER_LINE_UNKNOWN = "gender: unknown (unknown)"
STATS_PATTERN = (
    "model=fake-model input_tokens=321 output_tokens=17 examples=0/0 messages=3/3 "
    "prompt_characters="
)


def _command(
    repos: Repositories,
    client: ScriptedLLMClient,
    *,
    history_limit: int = HISTORY_LIMIT,
    budget: int = BIG_BUDGET,
) -> ReplyPreviewCommand:
    return ReplyPreviewCommand(
        repos.generator(client, history_limit=history_limit, budget=budget),
        MODEL,
        repos.contacts,
        GenderResolver(),
    )


async def test_the_reply_is_printed_on_its_own_line_followed_by_one_stats_line(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)

    code = await _command(repos, ScriptedLLMClient(f'"{REPLY_MARKER}"')).run(
        ReplyPreviewRequest(contact.id)
    )

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert code == 0
    assert captured.err == ""
    assert len(lines) == 3
    assert lines[0] == REPLY_MARKER
    assert lines[1] == GENDER_LINE_UNKNOWN
    assert lines[2].startswith(STATS_PATTERN)
    assert lines[2].removeprefix(STATS_PATTERN).isdigit()


async def test_the_stats_line_counts_examples_kept_of_offered(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)
    await repos.add_examples(3)

    await _command(repos, ScriptedLLMClient("fine")).run(ReplyPreviewRequest(contact.id))

    assert "examples=3/3 messages=3/3" in capsys.readouterr().out


async def test_show_prompt_prints_a_warning_then_both_messages_then_the_reply(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)
    client = ScriptedLLMClient(REPLY_MARKER)

    code = await _command(repos, client).run(ReplyPreviewRequest(contact.id, show_prompt=True))

    captured = capsys.readouterr()
    system, user = (message.content for message in client.calls[0][0])
    assert code == 0
    assert captured.err == f"{PERSONAL_DATA_WARNING}\n"
    assert len(captured.err.splitlines()) == 1
    stats = captured.out.splitlines()[-1]
    assert captured.out == (
        f"[system]\n{system}\n[user]\n{user}\n{REPLY_MARKER}\n{GENDER_LINE_UNKNOWN}\n{stats}\n"
    )
    assert stats.startswith(STATS_PATTERN)


async def test_show_prompt_shows_the_stored_summary_layer(
    repos: Repositories, session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)
    moment = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    stored = StoredSummary(
        version=SUMMARY_VERSION,
        updated_at=moment,
        cursor=SummaryCursor(created_at=moment, message_id=1),
        data=ContactSummary(told_about_owner=["preview-told-marker-zq"]),
    )
    await SqlSummaryRepository(session_factory).store_summary(contact.id, stored, None)

    code = await _command(repos, ScriptedLLMClient(REPLY_MARKER)).run(
        ReplyPreviewRequest(contact.id, show_prompt=True)
    )

    out = capsys.readouterr().out
    assert code == 0
    assert f"{SUMMARY_HEADER}\n{SUMMARY_MEMORY_NOTE}" in out
    assert "- preview-told-marker-zq" in out


async def test_without_show_prompt_the_prompt_is_not_printed(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)

    await _command(repos, ScriptedLLMClient("fine")).run(ReplyPreviewRequest(contact.id))

    captured = capsys.readouterr()
    for marker in (PERSONA_MARKER, MESSAGE_MARKER, BIO_MARKER):
        assert_absent(marker, captured.out + captured.err)


async def test_a_multi_line_reply_prints_one_numbered_line_per_part_then_the_stats(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)

    code = await _command(repos, ScriptedLLMClient("one\n\ntwo")).run(
        ReplyPreviewRequest(contact.id)
    )

    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert lines[:2] == ["[1/2] one", "[2/2] two"]
    assert lines[2] == GENDER_LINE_UNKNOWN
    assert lines[3].startswith(STATS_PATTERN)
    assert len(lines) == 4


async def test_more_lines_than_parts_print_the_merged_last_part(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)

    await _command(repos, ScriptedLLMClient("one\ntwo\nthree\nfour")).run(
        ReplyPreviewRequest(contact.id)
    )

    lines = capsys.readouterr().out.splitlines()
    assert lines[:3] == ["[1/3] one", "[2/3] two", "[3/3] three four"]
    assert len(lines) == 5


async def test_a_one_line_reply_keeps_the_unnumbered_line(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)

    await _command(repos, ScriptedLLMClient("just one line")).run(ReplyPreviewRequest(contact.id))

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "just one line"
    assert len(lines) == 3


async def test_the_command_writes_nothing_and_changes_no_row(
    repos: Repositories, migrated_url: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)
    before = await row_counts(migrated_url)
    stored = await repos.contacts.get_by_id(contact.id)
    history = await repos.messages.list_last(contact.id, 50)

    await _command(repos, ScriptedLLMClient("fine", "fine")).run(
        ReplyPreviewRequest(contact.id, show_prompt=True)
    )
    await _command(repos, ScriptedLLMClient("")).run(ReplyPreviewRequest(contact.id))

    capsys.readouterr()
    assert await row_counts(migrated_url) == before
    assert await repos.contacts.get_by_id(contact.id) == stored
    assert await repos.messages.list_last(contact.id, 50) == history


async def test_a_named_mode_is_used(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    await repos.modes.create(ModeCreate(name="playful", prompt_fragment="playful-fragment-zq"))
    contact = await ready_contact(repos)
    client = ScriptedLLMClient("fine")

    code = await _command(repos, client).run(ReplyPreviewRequest(contact.id, mode="playful"))

    assert code == 0
    assert "playful-fragment-zq" in client.calls[0][0][0].content
    capsys.readouterr()


async def test_a_disabled_named_mode_falls_back_to_the_default(
    repos: Repositories, migrated_url: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    await repos.modes.create(ModeCreate(name="playful", prompt_fragment="playful-fragment-zq"))
    await execute(migrated_url, "UPDATE modes SET enabled = false WHERE name = 'playful'")
    contact = await ready_contact(repos)
    client = ScriptedLLMClient("fine")

    code = await _command(repos, client).run(ReplyPreviewRequest(contact.id, mode="playful"))

    default = await repos.modes.get_by_name("default")
    assert default is not None
    assert code == 0
    assert default.prompt_fragment in client.calls[0][0][0].content
    assert "playful-fragment-zq" not in client.calls[0][0][0].content
    capsys.readouterr()


async def _failure(
    command: ReplyPreviewCommand, request: ReplyPreviewRequest, capsys: pytest.CaptureFixture[str]
) -> str:
    code = await command.run(request)
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    for marker in (PERSONA_MARKER, MESSAGE_MARKER, BIO_MARKER, USERNAME_MARKER, REPLY_MARKER):
        assert_absent(marker, captured.err)
    assert_absent(FAKE_TELEGRAM_ID, captured.err)
    return captured.err


async def test_an_unknown_contact(repos: Repositories, capsys: pytest.CaptureFixture[str]) -> None:
    await repos.personas.replace(PERSONA_MARKER)

    err = await _failure(
        _command(repos, ScriptedLLMClient("fine")), ReplyPreviewRequest(424242), capsys
    )

    assert err.startswith("reply-preview failed: ContactNotFound")


async def test_no_persona(repos: Repositories, capsys: pytest.CaptureFixture[str]) -> None:
    contact = await repos.contact()
    await repos.conversation(contact.id)

    err = await _failure(
        _command(repos, ScriptedLLMClient("fine")), ReplyPreviewRequest(contact.id), capsys
    )

    assert err.startswith("reply-preview failed: PersonaMissing")


async def test_no_incoming_messages(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    await repos.personas.replace(PERSONA_MARKER)
    contact = await repos.contact()

    err = await _failure(
        _command(repos, ScriptedLLMClient("fine")), ReplyPreviewRequest(contact.id), capsys
    )

    assert err.startswith("reply-preview failed: NoMessagesToReplyTo")


async def test_a_mode_that_does_not_exist(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)

    err = await _failure(
        _command(repos, ScriptedLLMClient("fine")),
        ReplyPreviewRequest(contact.id, mode="no-such-mode"),
        capsys,
    )

    assert err.startswith("reply-preview failed: ModeNameNotFound")
    assert "no-such-mode" not in err


async def test_no_default_mode(
    repos: Repositories, migrated_url: URL, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)
    await execute(migrated_url, "UPDATE modes SET enabled = false WHERE name = 'default'")

    err = await _failure(
        _command(repos, ScriptedLLMClient("fine")), ReplyPreviewRequest(contact.id), capsys
    )

    assert err.startswith("reply-preview failed: DefaultModeUnavailable")


async def test_a_prompt_that_is_too_large(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)

    err = await _failure(
        _command(repos, ScriptedLLMClient("fine"), budget=50),
        ReplyPreviewRequest(contact.id),
        capsys,
    )

    assert err.startswith("reply-preview failed: PromptTooLarge")


async def test_an_invalid_reply(repos: Repositories, capsys: pytest.CaptureFixture[str]) -> None:
    contact = await ready_contact(repos)

    err = await _failure(
        _command(repos, ScriptedLLMClient(f"leak {DIALOGUE_BOUNDARY}")),
        ReplyPreviewRequest(contact.id),
        capsys,
    )

    assert err.startswith("reply-preview failed: ReplyInvalid")
    assert DIALOGUE_BOUNDARY not in err


@pytest.mark.parametrize(
    ("failure", "name"),
    [
        (LLMAuthError("provider says secret-body", status_code=401), "LLMAuthError"),
        (LLMUnavailableError("provider says secret-body"), "LLMUnavailableError"),
    ],
)
async def test_an_llm_error_prints_the_class_and_never_its_message(
    repos: Repositories,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    name: str,
) -> None:
    assert isinstance(failure, LLMAuthError | LLMUnavailableError)
    contact = await ready_contact(repos)

    err = await _failure(
        _command(repos, ScriptedLLMClient(failure)), ReplyPreviewRequest(contact.id), capsys
    )

    assert err.startswith(f"reply-preview failed: {name}")
    assert "secret-body" not in err


async def test_with_show_prompt_a_late_error_still_exits_with_one(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)

    code = await _command(repos, ScriptedLLMClient(LLMUnavailableError("down"))).run(
        ReplyPreviewRequest(contact.id, show_prompt=True)
    )

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out.startswith("[system]\n")
    assert captured.err.splitlines()[-1].startswith("reply-preview failed: LLMUnavailableError")


def test_the_contact_id_must_be_a_number(
    full_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as caught:
        main(["reply-preview", "not-a-number"])

    assert caught.value.code == 2


def test_the_contact_id_is_required(
    full_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as caught:
        main(["reply-preview"])

    assert caught.value.code == 2


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("REPLY_HISTORY_MESSAGES", "0"),
        ("REPLY_HISTORY_MESSAGES", "101"),
        ("REPLY_HISTORY_MESSAGES", "many"),
        ("REPLY_PROMPT_BUDGET_CHARS", "3999"),
        ("REPLY_PROMPT_BUDGET_CHARS", "200001"),
        ("REPLY_PROMPT_BUDGET_CHARS", "-1"),
    ],
)
def test_invalid_settings_exit_with_two_and_name_the_variable(
    full_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(name, value)

    code = main(["reply-preview", "1"])

    captured = capsys.readouterr()
    assert code == 2
    assert name in captured.err
    assert f": {value}" not in captured.err
    assert captured.out == ""


def test_without_a_configuration_the_command_exits_with_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    assert main(["reply-preview", "1"]) == 2
    assert "Invalid configuration" in capsys.readouterr().err


def _mock_provider(monkeypatch: pytest.MonkeyPatch, script: Script) -> None:
    class MockedFactory(LLMClientFactory):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings, http_transport=httpx.MockTransport(script))

    monkeypatch.setattr(main_module, "LLMClientFactory", MockedFactory)


async def test_main_runs_the_whole_flow_against_the_database_and_a_mocked_provider(
    repos: Repositories,
    migrated_url: URL,
    full_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    contact = await ready_contact(repos)
    before = await row_counts(migrated_url)
    script = Script(
        completion_response(f'"{REPLY_MARKER}"', prompt_tokens=400, completion_tokens=9)
    )
    _mock_provider(monkeypatch, script)

    code = await asyncio.to_thread(main, ["reply-preview", str(contact.id), "--show-prompt"])

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert code == 0
    assert captured.err == f"{PERSONAL_DATA_WARNING}\n"
    assert lines[-3] == REPLY_MARKER
    assert lines[-2] == GENDER_LINE_UNKNOWN
    assert lines[-1].startswith(
        "model=deepseek-chat input_tokens=400 output_tokens=9 examples=0/0 messages=3/3 "
    )
    sent = script.sent_messages()
    assert [message["role"] for message in sent] == ["system", "user"]
    assert PERSONA_MARKER in sent[0]["content"]
    assert await row_counts(migrated_url) == before


async def test_main_uses_the_history_and_budget_settings(
    repos: Repositories,
    full_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPLY_HISTORY_MESSAGES", "2")
    await repos.personas.replace(PERSONA_MARKER)
    contact = await repos.contact()
    await repos.conversation(contact.id, count=6)
    _mock_provider(monkeypatch, Script(completion_response("fine")))

    code = await asyncio.to_thread(main, ["reply-preview", str(contact.id)])

    assert code == 0
    assert "messages=2/2" in capsys.readouterr().out


async def test_main_reports_a_rejected_key_with_the_class_only(
    repos: Repositories,
    full_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    contact = await ready_contact(repos)
    _mock_provider(monkeypatch, Script(error_response(401)))

    code = await asyncio.to_thread(main, ["reply-preview", str(contact.id)])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.startswith("reply-preview failed: LLMAuthError")
    assert "sk-deepseek-secret" not in captured.err
    assert "secret body" not in captured.err


def test_main_reports_an_unreachable_database_and_exits_with_one(
    full_env: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://nobody:pw-secret@127.0.0.1:1/none")
    _mock_provider(monkeypatch, Script(completion_response("fine")))

    code = main(["reply-preview", "1"])

    captured = capsys.readouterr()
    assert code == 1
    assert "storage failed" in captured.err
    assert "pw-secret" not in captured.out + captured.err


@pytest.mark.parametrize("raw", ["NEEDS_OWNER", "`needs_owner`", f"{REPLY_MARKER} NEEDS_OWNER"])
async def test_the_model_signal_prints_the_needs_owner_line_and_the_stats_and_exits_zero(
    repos: Repositories, capsys: pytest.CaptureFixture[str], raw: str
) -> None:
    contact = await ready_contact(repos)

    code = await _command(repos, ScriptedLLMClient(raw)).run(ReplyPreviewRequest(contact.id))

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert code == 0
    assert captured.err == ""
    assert lines[0] == NEEDS_OWNER_LINE == "needs owner (model signal)"
    assert lines[1] == GENDER_LINE_UNKNOWN
    assert lines[2].startswith(STATS_PATTERN)
    assert len(lines) == 3
    assert_absent(REPLY_MARKER, captured.out)


def _summary_with_gender(moment: datetime, gender: ContactGender) -> StoredSummary:
    return StoredSummary(
        version=SUMMARY_VERSION,
        updated_at=moment,
        cursor=SummaryCursor(created_at=moment, message_id=1),
        data=ContactSummary(gender=gender),
    )


async def _preview_lines(
    repos: Repositories,
    contact_id: int,
    capsys: pytest.CaptureFixture[str],
    *,
    default: ContactGender | None = None,
) -> tuple[list[str], str]:
    client = ScriptedLLMClient(REPLY_MARKER)
    command = ReplyPreviewCommand(
        repos.generator(client, default_gender=default),
        MODEL,
        repos.contacts,
        GenderResolver(default),
    )

    code = await command.run(ReplyPreviewRequest(contact_id))

    assert code == 0
    system = client.calls[0][0][0].content
    return capsys.readouterr().out.splitlines(), system


async def test_the_preview_line_and_the_prompt_agree_for_an_owner_choice(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)
    await repos.contacts.set_gender(contact.id, ContactGender.MALE)

    lines, system = await _preview_lines(repos, contact.id, capsys)

    assert lines[1] == "gender: male (owner)"
    assert "- Grammatical gender: male" in system


async def test_the_preview_line_and_the_prompt_agree_for_a_summary_gender(
    repos: Repositories, session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)
    moment = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    await SqlSummaryRepository(session_factory).store_summary(
        contact.id, _summary_with_gender(moment, ContactGender.FEMALE), None
    )

    lines, system = await _preview_lines(repos, contact.id, capsys)

    assert lines[1] == "gender: female (summary)"
    assert "- Grammatical gender: female" in system


async def test_the_preview_line_and_the_prompt_agree_for_a_name_guess(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)
    await repos.contacts.update_identity(contact.id, USERNAME_MARKER, "Анна")

    lines, system = await _preview_lines(repos, contact.id, capsys)

    assert lines[1] == "gender: female (name)"
    assert "- Grammatical gender: female" in system


async def test_the_preview_line_and_the_prompt_agree_for_the_default_setting(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)

    lines, system = await _preview_lines(repos, contact.id, capsys, default=ContactGender.MALE)

    assert lines[1] == "gender: male (default)"
    assert "- Grammatical gender: male" in system


async def test_the_preview_line_and_the_prompt_agree_when_nothing_is_known(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)

    lines, system = await _preview_lines(repos, contact.id, capsys)

    assert lines[1] == "gender: unknown (unknown)"
    assert "- Grammatical gender: unknown" in system


async def test_the_gender_line_never_carries_a_name_or_a_message(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)

    lines, _ = await _preview_lines(repos, contact.id, capsys)

    assert_absent(USERNAME_MARKER, lines[1])
    assert_absent(MESSAGE_MARKER, lines[1])
    assert_absent("Contactfirst", lines[1])


SKIP_PREVIEW_LINE = "skip (model signal)"
COUNT_STATEMENTS = (
    "SELECT count(*) FROM scheduled_messages",
    "SELECT count(*) FROM messages",
    "SELECT count(*) FROM contacts",
)
PROACTIVE_PART_ONE = "proactive-part-one-zq"
PROACTIVE_PART_TWO = "proactive-part-two-zq"


async def test_the_proactive_preview_prints_the_parts_and_the_stats(
    repos: Repositories, session_factory: SessionFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)
    client = ScriptedLLMClient(f"{PROACTIVE_PART_ONE}\n{PROACTIVE_PART_TWO}")
    before = await row_counts_of(session_factory)

    code = await _command(repos, client).run(ReplyPreviewRequest(contact.id, proactive=True))

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert code == 0
    assert captured.err == ""
    assert lines[:2] == [f"[1/2] {PROACTIVE_PART_ONE}", f"[2/2] {PROACTIVE_PART_TWO}"]
    assert lines[2] == GENDER_LINE_UNKNOWN
    assert lines[3].startswith(STATS_PATTERN)
    assert "reply with exactly SKIP" in client.calls[0][0][1].content
    assert "reply with exactly SKIP" in client.calls[0][0][0].content
    assert await row_counts_of(session_factory) == before


@pytest.mark.parametrize("raw", ["SKIP", "`skip`", f"{REPLY_MARKER} SKIP"])
async def test_the_proactive_preview_prints_the_skip_line_and_exits_zero(
    repos: Repositories, capsys: pytest.CaptureFixture[str], raw: str
) -> None:
    contact = await ready_contact(repos)

    code = await _command(repos, ScriptedLLMClient(raw)).run(
        ReplyPreviewRequest(contact.id, proactive=True)
    )

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert code == 0
    assert lines[0] == SKIP_PREVIEW_LINE
    assert lines[1] == GENDER_LINE_UNKNOWN
    assert lines[2].startswith(STATS_PATTERN)
    assert len(lines) == 3
    assert_absent(REPLY_MARKER, captured.out)


async def test_a_skip_in_a_normal_preview_is_a_refused_reply(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)

    code = await _command(repos, ScriptedLLMClient("SKIP")).run(ReplyPreviewRequest(contact.id))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "ReplyInvalid" in captured.err


def test_the_command_line_reads_the_proactive_flag() -> None:
    args = main_module._parse_args(["reply-preview", "12", "--proactive"])
    plain = main_module._parse_args(["reply-preview", "12"])

    assert reply_preview_request(args) == ReplyPreviewRequest(12, proactive=True)
    assert reply_preview_request(plain) == ReplyPreviewRequest(12)


async def row_counts_of(session_factory: SessionFactory) -> list[int]:
    counts = []
    async with session_factory() as session:
        for statement in COUNT_STATEMENTS:
            counts.append(int((await session.execute(text(statement))).scalar_one()))
    return counts


BERLIN_ZONE = ZoneInfo("Europe/Berlin")
BAD_AT_VALUES = ["tomorrow-zq", "2026-11-04", "2026-13-04 10:00", "04.11.2026 10:00"]
WRAPUP_PROFILE = ScheduleProfile.model_validate(
    {
        "version": 1,
        "timezone": "Europe/Berlin",
        "night": {"start": ["01:00", "01:00"]},
        "busy": [{"days": ["wed"], "from": "14:00", "to": "15:00", "label": "лекция"}],
    }
)


def _wrapup_command(
    repos: Repositories, client: ScriptedLLMClient, *, wrapup_enabled: bool = True
) -> ReplyPreviewCommand:
    clock = PreviewClock(lambda: datetime(2026, 11, 4, 9, 0, tzinfo=UTC))
    generator = ReplyGenerator(
        contacts=repos.contacts,
        messages=repos.messages,
        modes=repos.modes,
        persona=PersonaService(repos.personas),
        examples=ExampleSelector(repos.style_examples, 8),
        gender=GenderResolver(),
        builder=PromptBuilder(
            budget_chars=BIG_BUDGET, timezone=ZoneInfo("Europe/Berlin"), clock=clock
        ),
        client=client,
        history_limit=HISTORY_LIMIT,
    )
    return ReplyPreviewCommand(
        generator,
        MODEL,
        repos.contacts,
        GenderResolver(),
        schedule=ScheduleService(InMemoryScheduleProfiles(WRAPUP_PROFILE), "UTC"),
        clock=clock,
        wrapup_enabled=wrapup_enabled,
    )


@pytest.mark.parametrize(
    ("at", "line"),
    [
        ("2026-11-04 00:48", "wrap-up: hard, 12 min, sleep"),
        ("2026-11-04 00:30", "wrap-up: soft, 30 min, sleep"),
        ("2026-11-04 13:30", "wrap-up: soft, 30 min, лекция"),
    ],
)
async def test_the_preview_prints_the_wrapup_line_before_the_stats(
    repos: Repositories, capsys: pytest.CaptureFixture[str], at: str, line: str
) -> None:
    contact = await ready_contact(repos)
    client = ScriptedLLMClient("спокойной ночи")

    code = await _wrapup_command(repos, client).run(ReplyPreviewRequest(contact.id, at=at))

    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert lines[1] == GENDER_LINE_UNKNOWN
    assert lines[2] == line
    assert lines[3].startswith(STATS_PATTERN)
    user = client.calls[0][0][1].content
    assert user.splitlines()[0].startswith(f"Current local time: Wednesday, {at} ")
    assert "The owner becomes unavailable in about" in user.splitlines()[1]


@pytest.mark.parametrize("at", ["2026-11-04 03:00", "2026-11-04 14:30", "2026-11-04 18:00"])
async def test_a_time_inside_or_far_from_a_window_gives_no_wrapup_line(
    repos: Repositories, capsys: pytest.CaptureFixture[str], at: str
) -> None:
    contact = await ready_contact(repos)
    client = ScriptedLLMClient("fine")

    await _wrapup_command(repos, client).run(ReplyPreviewRequest(contact.id, at=at))

    out = capsys.readouterr().out
    assert "wrap-up:" not in out
    assert "becomes unavailable" not in client.calls[0][0][1].content


async def test_with_the_switch_off_the_preview_gives_no_wrapup_line(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)

    await _wrapup_command(repos, ScriptedLLMClient("fine"), wrapup_enabled=False).run(
        ReplyPreviewRequest(contact.id, at="2026-11-04 00:48")
    )

    assert "wrap-up:" not in capsys.readouterr().out


async def test_a_skip_inside_the_wrapup_time_prints_the_skip_line(
    repos: Repositories, capsys: pytest.CaptureFixture[str]
) -> None:
    contact = await ready_contact(repos)

    code = await _wrapup_command(repos, ScriptedLLMClient("SKIP")).run(
        ReplyPreviewRequest(contact.id, at="2026-11-04 00:48")
    )

    assert code == 0
    assert capsys.readouterr().out.splitlines()[0] == SKIP_PREVIEW_LINE


@pytest.mark.parametrize(
    "at", ["tomorrow-zq", "2026-11-04", "2026-13-04 10:00", "04.11.2026 10:00"]
)
async def test_a_bad_at_is_one_readable_line_and_exit_one(
    repos: Repositories, capsys: pytest.CaptureFixture[str], at: str
) -> None:
    contact = await ready_contact(repos)
    client = ScriptedLLMClient("fine")

    code = await _wrapup_command(repos, client).run(ReplyPreviewRequest(contact.id, at=at))

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == 'reply-preview failed: bad --at, use "YYYY-MM-DD HH:MM"\n'
    assert client.calls == []


async def test_the_preview_with_at_writes_nothing(
    repos: Repositories, session_factory: SessionFactory
) -> None:
    contact = await ready_contact(repos)
    before = await row_counts_of(session_factory)

    await _wrapup_command(repos, ScriptedLLMClient("SKIP")).run(
        ReplyPreviewRequest(contact.id, at="2026-11-04 00:48")
    )

    assert await row_counts_of(session_factory) == before
    stored = await repos.contacts.get_by_id(contact.id)
    assert stored is not None and stored.reply_after is None


def test_the_command_line_reads_the_at_option() -> None:
    args = main_module._parse_args(["reply-preview", "12", "--at", "2026-11-04 00:48"])

    assert reply_preview_request(args) == ReplyPreviewRequest(12, at="2026-11-04 00:48")
