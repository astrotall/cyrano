# CLAUDE.md

Read this file first, then the guides in [.claude/docs](.claude/docs) before touching code.

## Product

Cyrano is a Telegram assistant that chats with contacts approved by its owner, in the owner's
style, through an LLM.

- A **Telethon userbot** on a second Telegram account is what contacts talk to.
- An **aiogram control bot** on the owner's main account is the control panel.
- An **Arq worker** handles delayed jobs.
- People come from a matching bot. The owner likes profiles there by hand; the system never
  touches the matching bot.

Main risks: replying to the wrong person, replying where a human must decide, account limits and
bans, leaking personal data. Therefore: the owner approves every contact, only approved contacts
get automatic replies, one guarded sender is the only way out, stop-triggers hand over to the
owner, and pacing and limits are enforced centrally.

Contact statuses: `new -> pending -> active -> paused / ignored`. Meaning and transitions:
[contact-lifecycle.md](.claude/docs/contact-lifecycle.md).

What exists now:

- The skeleton (CYR-8) and the storage layer (CYR-9): typed configuration, JSON logging, the app
  process with a heartbeat, the Arq worker, Docker Compose, CI, the schema with its migrations and
  the repositories.
- The userbot intake (CYR-10): unknown private senders become `new` contacts and their messages are
  stored with Telegram's send time (a missing or naive time, and a time more than 5 minutes ahead of the
  clock, becomes now; CYR-40 applies the same rule to the owner's own messages); "newer" is decided by
  `(created_at, id)`.
- The profile collection (CYR-11) and the control bot with the contact card and its Reply and Ignore
  buttons (CYR-12).
- The LLM client (CYR-13), the persona and the style examples with their import (CYR-14) and the
  chat export converter (CYR-34). The layered reply prompt, the reply generator and the
  `reply-preview` command (CYR-16).
- The outbox with the one sender and the status guard (CYR-19), its limits, flood-wait pause and
  account-level stop (CYR-22) and the humanization: the send time planner, the schedule profile, and
  the read mark, typing and availability check in the sender (CYR-18).
- The pause on a manual message of the owner (CYR-21) and the stop-triggers with the hand-over
  and the `NEEDS_OWNER` model signal (CYR-20).
- The autopilot (CYR-17), behind `AUTOPILOT_ENABLED` (default off): the app enqueues one Arq job per
  incoming message of an `active` contact, the worker generates one reply per burst with the persona,
  the examples and the reply generator and writes an `autopilot` outbox row, and the app's sender sends
  it (one reply per burst by supersession, CYR-41; the adaptive debounce, the maximum wait and replies
  that finish once started, CYR-43). The sweeper, `reply_after`, the autopilot stop and
  cooldown and the `autopilot` command belong to it.
- Multi-line replies (CYR-36): the model puts each message on its own line, the job splits the reply
  into at most three parts and stores them as one chain of outbox rows (`follows_id`), and the sender
  sends the parts in order, a short pause apart; the limit notice names the limit, its setting and its
  numbers.
- The contact summaries (CYR-25): the app's sweeper enqueues a summary job every
  `SUMMARY_EVERY_MESSAGES` messages of an `active` or `paused` contact (independent of the autopilot,
  behind `SUMMARY_ENABLED`, default on), the worker distils the next chunk of the dialogue into a
  structured, sanitised summary on the contact with a compare-and-set on its cursor, and the reply
  generator shows it as the summary layer, `told_about_owner` included.
- The early read (CYR-44), behind `EARLY_READ_ENABLED` (default on) and a running autopilot: in a lively
  conversation of an `active` contact, outside the night and busy windows, the intake registers a request
  and the sender's second coroutine marks the chat read soon after the message (the profile's
  `early_read_seconds`, default 0 to 1 s); only the read mark moves earlier.
- The owner's commands: `login`, `llm-check`, `persona`, `examples`, `reply-preview`, `outbox`,
  `schedule`, `stop-check`, `autopilot` and `summary` (the list is in the README).

Still planned, each marked with its ticket key where there is one: conversation modes beyond the
default one (CYR-15), the rest of the control panel (CYR-26), proactive messages with their limits (CYR-23), the other background
jobs of the worker (CYR-23, CYR-24) and a catalog of contacts (no ticket yet).
Never describe planned behaviour as existing.

## Locked architectural decisions

Changing any of these is a large change (see Workflow expectations). Reasons:
[decisions.md](.claude/docs/decisions.md).

| Decision | Choice                                                                                                                                                                                         | Consequence |
| --- |------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------| --- |
| Users | One owner; the control bot answers only `OWNER_TELEGRAM_ID`                                                                                                                                    | No multi-user code, no roles; the control bot gives nobody else an answer (CYR-12) |
| Likes | The owner likes in the matching bot by hand                                                                                                                                                    | No automation of the matching bot, ever |
| Automatic replies | Only to contacts in status `active`; checked at scheduling time and again right before sending                                                                                                 | A status change between the two checks cancels the reply |
| One way out | Every outgoing message goes through the sender                                                                                                                                                 | No other code path sends; the status guard lives in the sender |
| Hand-over | Meeting or date proposals, phone and contact exchange, money, photo requests, a direct question whether it is a bot or AI, the owner's exes (an explicit owner decision taken in CYR-20), crisis topics (the same decision) and low model confidence, which the model signals with `NEEDS_OWNER`, stop the conversation and notify the owner. The text check runs at intake for `active` contacts and again on the whole burst in the autopilot job | The assistant never answers those itself |
| Style | Persona text plus few-shot examples from the owner's real chats; no model training. Modes are a prompt fragment plus examples; the persona is common                                           | Style changes are text and data changes, not code or training |
| LLM | One interface, DeepSeek first (plain httpx, no SDK), provider replaceable by config                                                                                                            | No provider SDK; the rest of the code sees only the interface |
| Infra | PostgreSQL, async SQLAlchemy 2.x, Alembic, Redis, Arq, Docker Compose, Python 3.14, uv                                                                                                         | Everything async; schema changes go through Alembic |
| Data | Real personal data (persona, examples, sessions, media, messages) never goes to git; fixtures use fake data only                                                                               | `.env`, sessions, `media/` and `data/` stay git-ignored |
| Logs | Message text, names and other personal data are never logged at INFO or above                                                                                                                  | Logs carry internal ids, counts and statuses, not content; the startup line carries an allowlist of non-personal settings, never secrets or Telegram ids |

## Stack

| Technology | State |
| --- | --- |
| Python 3.14, uv | in the project |
| pydantic-settings (Pydantic v2), tzdata | in the project |
| Arq, Redis 7.4 | in the project (the reply job of the autopilot and the summary job; only `worker/` imports arq) |
| PostgreSQL 16.15 | in the project (compose, migrations, tests) |
| SQLAlchemy 2.x async, asyncpg, Alembic | in the project (schema, repositories, used by the intake) |
| Ruff, mypy strict, pytest, pytest-asyncio | in the project |
| Docker, Docker Compose, GitHub Actions, Dependabot | in the project |
| Telethon | in the project (userbot listens, registers contacts, reads profiles and sends only through `userbot/adapter.py` for the sender; imported only in `userbot/`) |
| aiogram 3 | in the project (control bot; imported only in `bot/`) |
| DeepSeek client over plain httpx | in the project (`llm/`; the only package that imports httpx; built by the worker and two commands, not by `main.run`) |
| Persona and style examples | in the project (imported from local files into PostgreSQL by commands; selectors in `services/`; used by the worker's reply job, not by `main.run`) |
| Layered reply prompt | in the project (`prompts/` for texts, builder and generator in `services/`; used by `reply-preview` and the worker's reply job) |
| httpx.MockTransport in tests | in the project (no respx) |

## Repository layout

```
.
├── CLAUDE.md, CONTRIBUTING.md, README.md
├── pyproject.toml, uv.lock, .python-version
├── Makefile                      includes login, login-docker, llm-check, persona-import, examples-import, examples-stats, examples-convert, reply-preview, outbox-enqueue, outbox-list, outbox-cancel, outbox-retry, outbox-status, outbox-resume, schedule-import, schedule-show, schedule-check, schedule-plan, stop-check, autopilot-status, autopilot-resume, summary-update, summary-show, summary-clear and setup-hooks
├── Dockerfile, docker-compose.yml, docker-compose.override.yml
├── .env.example, .dockerignore, .gitignore
├── alembic.ini
├── migrations/                   async env.py, versions/
├── examples/                     invented sample persona, style examples and schedule (real files go to data/)
├── .claude/docs/                 architecture, contact-lifecycle, safety-rules, decisions, persona-and-examples, reply-generation, outbox-and-sender, humanization, stop-triggers, autopilot, summaries
├── .githooks/                    pre-commit (runs make check and make test; enabled by make setup-hooks)
├── .github/                      dependabot.yml, workflows/ci.yml
├── src/cyrano/
│   ├── __main__.py, main.py      entrypoint (composition root), signals, heartbeat, subcommands login, llm-check, persona (import, show), examples (import, stats, convert), reply-preview, outbox (enqueue, list, cancel, retry, status, resume), schedule (import, show, check, plan), stop-check, autopilot (status, resume), summary (update, show, clear)
│   ├── style_cli.py              the persona and examples commands
│   ├── convert_cli.py            the examples convert command (chat export to a draft)
│   ├── reply_cli.py              the reply-preview command
│   ├── outbox_cli.py             the outbox enqueue, list, cancel, retry, status and resume commands
│   ├── schedule_cli.py           the schedule import, show, check and plan commands
│   ├── stop_check_cli.py         the stop-check command (a dry run of the detector)
│   ├── autopilot_cli.py          the autopilot status and resume commands
│   ├── summary_cli.py            the summary update, show and clear commands
│   ├── stop_trigger_patterns.py  the stop-trigger pattern data (imports only domain)
│   ├── config.py                 Settings, load_settings, ConfigError
│   ├── logging.py                JSON-lines logging
│   ├── health.py                 heartbeat file and the app healthcheck
│   ├── domain/                   enums, status transition rules, errors, Pydantic models
│   ├── db/                       SQLAlchemy models, engine and sessions, repositories
│   ├── services/                 business logic; every module:
│   │                             repositories (the repository Protocols), intake (registration of incoming messages),
│   │                             message_time (the one rule for the stored time of a message, both directions),
│   │                             sanitize (NUL-byte removal), contact_events (the in-memory event queue),
│   │                             profile_source (the profile reader Protocols), leomatch (the matching-bot card parser),
│   │                             profile_collector (profile, photo and card per new contact),
│   │                             card_sender (the CardSender Protocol), card_builder (the owner's card),
│   │                             card_announcer (card to the owner, new -> pending), card_actions (Reply and Ignore),
│   │                             pause_actions (Resume and Ignore of a paused contact),
│   │                             llm_client (the LLMClient Protocol), llm_check (the request behind llm-check),
│   │                             style_files (persona and examples files), style_import (the importers),
│   │                             persona_service (the stored persona), style_examples (selection and statistics),
│   │                             chat_export_reader (Telegram Desktop export), chat_redaction (tokens for private data),
│   │                             chat_dialogues (dialogue candidates), chat_export_converter (export to a draft file),
│   │                             contact_facts (the profile facts that may reach a prompt),
│   │                             prompt_builder (the layered prompt and its budget), reply_postprocess (reply cleaning),
│   │                             reply_split (the parts of a multi-line reply),
│   │                             reply_generator (build, call the LLM, clean),
│   │                             send_guard (the status guard), send_limiter (the six send limits),
│   │                             outbox_service (validation and enqueue), outbox_sender (the one sender),
│   │                             message_transport (the write Protocol of the sender), owner_notifier (the owner notice Protocol),
│   │                             humanization (the send time planner and the schedule service),
│   │                             early_read (the early read requests: queue, gate, scheduler),
│   │                             schedule_files (the schedule file), manual_intervention (the manual message detector),
│   │                             manual_pause_notifier (the manual pause notice Protocol),
│   │                             stop_triggers (the pure detector), hand_over (pause, cancel and one notice),
│   │                             hand_over_notifier (the hand-over notice Protocol),
│   │                             reply_job (one generated reply per burst), autopilot (the scheduler and the sweeper),
│   │                             reply_job_queue (the job queue Protocol),
│   │                             summary_sanitizer (contact details out of a summary),
│   │                             summary_updater (one chunk into the stored summary),
│   │                             summary_sweeper (the summary scan), summary_job_queue (its queue Protocol)
│   ├── userbot/                  Telethon: protocols, filters, handler, client, adapter, login, runner, errors,
│   │                             profile_source, leomatch_source, leomatch_match
│   ├── bot/                      aiogram: app, middleware, handlers, rendering, callbacks, sender, notifier, messages, errors
│   ├── prompts/                  prompt texts and their pure rendering (imports only domain)
│   ├── llm/                      DeepSeek client, retrying transport, factory; the only package that imports httpx
│   └── worker/                   Arq: settings (WorkerSettings), jobs (reply_to_contact, update_contact_summary),
│                                 composition, queue (ArqReplyJobQueue, ArqSummaryJobQueue, used by the app);
│                                 the only package that imports arq
└── tests/                        mirrors src/cyrano
```

CYR-8 added the package skeleton, typed configuration, JSON logging, app process with heartbeat,
Arq worker skeleton, Docker Compose, CI and Dependabot. CYR-9 added `domain/`, the content of
`db/`, `services/repositories.py`, `alembic.ini`, `migrations/`, the one-shot `migrate` compose
service and the database tests. CYR-10 added `userbot/` (the only package that imports Telethon;
it listens and registers), `services/intake.py`, `services/sanitize.py`,
`services/contact_events.py`, the `IncomingMessage` and `NewContactEvent` models, the
`python -m cyrano login` command with the `make login` and `make login-docker` targets, and the
userbot tests with their fakes. CYR-11 added the profile collection: `services/profile_collector.py`
(`ProfileCollector`, started by `main.run` next to the userbot), `services/profile_source.py` (the
`ProfileSource` and `LeomatchSource` Protocols, `ProfileSourceError`), `services/leomatch.py` (the
card parser), the read-only Telethon adapters `userbot/profile_source.py`,
`userbot/leomatch_source.py` and `userbot/leomatch_match.py`, the `ContactProfile`,
`LeomatchProfile`, `TelegramProfile` and `ContactReadyEvent` models, `LeomatchLookup`,
`ContactRepository.update_identity`, the generic in-memory event queue (two instances), the
`LEOMATCH_BOT_USERNAME` setting, and the tests with their fakes. CYR-12 added the `aiogram`
dependency and the `bot/` package (the only one that imports aiogram: `ControlBot` with the polling,
the owner-only middleware, the handlers, the card rendering and keyboard, `TelegramCardSender`, the
Russian texts in `messages.py`, the readable fatal errors), `services/card_sender.py` (the
`CardSender` Protocol), `services/card_builder.py`, `services/card_announcer.py` (`CardAnnouncer`,
started by `main.run` next to the control bot), `services/card_actions.py` (`CardActions`), the
`ContactCard`, `CardLeomatch`, `ModeChoice`, `CardActionResult` models and the `CardOutcome` enum,
and the tests with their fakes (a recording aiogram session, `FakeControlBot`). CYR-13 added the
`httpx` dependency and the `llm/` package (`DeepSeekClient`, `RetryingTransport` with retries,
backoff and a per-attempt deadline, `LLMClientFactory` with one bound client per step, the JSON
reply parsing with validation retries, the constants and prompt texts), `services/llm_client.py`
(the `LLMClient` Protocol), `services/llm_check.py`, `domain/llm.py` (`LLMMessage`, `LLMUsage`,
`LLMResult`), the `Role` and `LLMStep` enums, the `LLMError` family in `domain/errors.py`, the
optional per-step `LLM_*`, `DEEPSEEK_BASE_URL` and `DEEPSEEK_THINKING` settings, the
`python -m cyrano llm-check` command with `make llm-check`, and the tests with their fakes
(`httpx.MockTransport` handlers, `FakeLLMClient`). The app process builds no client; the worker does.
CYR-14 added the persona and the style examples: `domain/style.py` (models, limits, the content
hash), the import errors in `domain/errors.py`, `services/style_files.py`, `style_import.py`
(`PersonaImporter`, `ExamplesImporter`), `persona_service.py` and `style_examples.py`
(`ExampleSelector`), the two repository Protocols, `SqlPersonaRepository` and
`SqlStyleExampleRepository`, migration `0002` (`persona`, `style_examples`), `style_cli.py` with the
`persona` and `examples` subcommands and their Makefile targets, the optional `STYLE_EXAMPLES_MAX`
setting, the invented samples in `examples/`, the `.gitignore` rules for real files, and the tests.
They are stored in PostgreSQL, not read from a path at runtime; the worker's reply job and `reply-preview` select them.
CYR-34 added the converter of a Telegram Desktop JSON chat export into a draft examples file:
`domain/chat_export.py` (limits, vocabulary, report), `services/chat_export_reader.py`,
`chat_redaction.py`, `chat_dialogues.py`, `chat_export_converter.py`, `convert_cli.py` (the
`examples convert` command with `--me` or `--me-id`; dispatched before `Settings` load, so it needs
no `.env` and no database), `ChatExportError`, the `examples-convert` Makefile target and the tests.
It reads a file and writes a file under `data/`; nothing in the app uses it.
CYR-16 added the `prompts/` package (`texts.py` with every fixed prompt text, `render.py` with the
pure rendering of the system and the user message), `domain/reply.py` (`ContactFacts`,
`BuiltPrompt`, `GeneratedReply`, the limits and refusal reasons), the errors `PersonaMissing`,
`ModeNameNotFound`, `NoMessagesToReplyTo`, `PromptTooLarge` and `ReplyInvalid`,
`services/contact_facts.py`, `prompt_builder.py` (`PromptBuilder`), `reply_postprocess.py`
(`clean_reply`) and `reply_generator.py` (`ReplyGenerator`), `reply_cli.py` with the
`python -m cyrano reply-preview` command and the `reply-preview` Makefile target, the optional
`REPLY_HISTORY_MESSAGES` and `REPLY_PROMPT_BUDGET_CHARS` settings, and the tests. The generator
builds and cleans one reply text; it stores nothing, sends nothing, and `main.run` does not use it.
CYR-19 added the outbox and the one sender: `domain/outbox.py`, the `OutboxState` and `OutboxSource`
enums, the outbox and send errors in `domain/errors.py`, the optional `AUTOPILOT_ENABLED` setting,
migration `0003` (`scheduled_messages`), `SqlOutboxRepository` and the `OutboxRepository` Protocol,
`services/send_guard.py`, `outbox_service.py`, `outbox_sender.py` (`OutboxSender`, started by `main.run`
after the userbot and the control bot and cancelled first; the only caller of `send_text`),
`message_transport.py` (three methods since CYR-18) and `owner_notifier.py` (one method), `userbot/adapter.py`
(`TelethonAdapter`, the only module with a Telethon write call; the rest of `userbot/` only listens
and reads), `UserbotAccess` and `build_access` (the client with its adapter), `get_full_user` on
`UserbotClient` (which has no generic request call), `bot/notifier.py`, `outbox_cli.py` with the
`outbox` commands and their Makefile targets, and the tests, with the structural scans in
`tests/test_one_sender.py`.
CYR-22 added the limits, the pause and the stop of the sender: the six optional `SEND_LIMIT_*`
settings with their ordering rules, migration `0004` (`sender_state`, one row, and the index on
`messages (direction, created_at)`), the `SenderState`, `SendLimit` and `OutgoingPeak` models,
`SendAccountBlocked`, `SenderStateMissing`, the `RejectionReason` carried by `SendRejected`, the
`SenderStateRepository` Protocol with `SqlSenderStateRepository`, `outgoing_times`,
`busiest_outgoing_contact` and `count_by_state`, `services/send_limiter.py`, the new steps of
`OutboxSender` (state read, stale rows, limits, sender-wide flood pause, account stop, contact paused
after a recipient rejection), the new owner notices, the `outbox status` and `outbox resume` commands
and their Makefile targets, and the tests, in `tests/services/test_send_limiter.py`,
`tests/services/test_outbox_sender_safety.py` and `tests/db/test_sender_state_repository.py`. Later in
the same ticket: migration `0005` (`due_at` on `scheduled_messages`, the originally intended send
time the 6 h stale rule counts from, unchanged by a reschedule), `OWNER_NOTIFY_DEADLINE` (every owner
notice of the sender has a 15 s deadline), the account-level errors extended to `FrozenMethodInvalidError`,
`AuthKeyDuplicatedError`, `AuthKeyPermEmptyError`, `ActiveUserRequiredError`,
`SessionPasswordNeededError` and a bare `UnauthorizedError`, the stale summary notice (one notice with
the count of rows cancelled as stale, at most hourly) and the stop reminder (every 6 h while the
sender is stopped).
CYR-18 added the humanization: `domain/schedule.py` (`ScheduleProfile` with its limits and defaults,
`Block`, `SendPlan`), the `Weekday`, `BlockKind` and `PlanReason` enums, `PlanNotPossible`,
`StoredScheduleInvalid`, `ScheduleTimezoneUnresolved`, `ERROR_OUTSIDE_HOURS`, migration `0006`
(`schedule_profile`, no seed row), the `ScheduleProfileRepository` Protocol with
`SqlScheduleProfileRepository`, `services/humanization.py` (the pure planner, `ScheduleService`,
`HumanizationPlanner`, called by the autopilot job since CYR-17), `services/schedule_files.py`, the `show_typing` and
`mark_read` methods of `MessageTransport` with their adapter implementation, the new steps of
`OutboxSender` (the availability check for autopilot rows; read mark, pause and typing for every row;
the second guard; `SENDING_STALE_AFTER` computed, 195 s), `schedule_cli.py` with the `schedule` commands and
their Makefile targets, `examples/schedule.sample.json`, the `schedule*.json` ignore rule, and the
tests. Splitting a reply came with CYR-36.
CYR-21 added the auto-pause on a manual message: the `PauseReason` enum and the nullable
`contacts.pause_reason` (migration `0007`, a check constraint, no backfill), the `pause_reason`
argument and the `changed` flag of `ContactRepository.set_status` (`StatusChange`), the recipient-rejection pause
now storing `send_rejected`, `MessageRepository.exists`, `OutboxRepository.has_sending_row`, the
`OutgoingMessage` model, the outcome enums, `services/manual_intervention.py` (`ManualMessageDetector`),
`manual_pause_notifier.py` (the one-method `ManualPauseNotifier` Protocol) and `pause_actions.py`
(`PauseActions`), `userbot/outgoing_handler.py` and `outgoing_event_rejection` (a second Telethon handler;
`IncomingEvent` gained the read members `chat_id` and `get_chat`), `TelegramManualPauseNotifier` in
`bot/notifier.py`, the `PauseCallback` and the resume handler, the new owner texts, the wiring in `main.py`,
and the tests, with the new scans in `tests/test_one_sender.py`.
CYR-20 added the stop-triggers: the `TriggerClass`, `TriggerSource`, `HandOverOutcome` and `IgnoreOutcome`
enums, `PauseReason.STOP_TRIGGER` and `CANCEL_REASON_STOP_TRIGGER`, migration `0008` (the check constraint
on `contacts.pause_reason` accepts `stop_trigger`; the downgrade turns it into `owner`),
`domain/stop_triggers.py` (`StopDecision`, `DialogueLine`, `HandOverNotice`, the limits),
`stop_trigger_patterns.py` (the pattern data), `services/stop_triggers.py` (the pure detector),
`services/hand_over.py` (`HandOverService`) and `hand_over_notifier.py` (the one-method `HandOverNotifier`
Protocol), the gate in `IntakeService` (the optional `hand_over` keyword), `PauseActions.ignore`, the
`NEEDS_OWNER_SIGNAL` instruction in `prompts/texts.py`, `GeneratedReply.needs_owner` and the signal check in
`reply_postprocess.py` and `reply_generator.py`, the `needs owner (model signal)` line of `reply-preview`,
`TelegramHandOverNotifier` with the notice rendering and the keep and ignore buttons, the new owner texts,
the wiring in `main.py` (the control bot is built before the intake), `stop_check_cli.py` with the
`stop-check` command and its Makefile target, and the tests, with the phrase lists in
`tests/services/stop_trigger_phrases.py` and the new scans in `tests/test_one_sender.py`.
CYR-17 added the autopilot: `domain/autopilot.py` (`AutopilotState`, `ReplyJobRequest`,
`ReplyJobResult`, every autopilot constant), the `AutopilotStopReason`, `AutopilotPauseReason`,
`ReplyJobOutcome` and `ReplyJobReason` enums, `RetryJob`, `ReplyAfterRefused` and
`AutopilotStateMissing`, migration `0009` (`contacts.reply_after`, the seeded one-row
`autopilot_state`), the `reply_after` argument of `ContactRepository.set_status` (passed by
`PauseActions.resume`), `MessageRepository.get`, `OutboxRepository.has_row_for_trigger` and the source
filter of `count_by_state`, the `AutopilotStateRepository` and `ReplyBacklogRepository` Protocols with
`SqlAutopilotStateRepository` and `SqlReplyBacklogRepository`, `GeneratedReply.truncated` and the
`max_tokens` override of `ReplyGenerator.generate`, `services/reply_job.py` (`ReplyJobRunner`,
`AutopilotNotices`), `services/autopilot.py` (`AutopilotScheduler`, called by the intake after the
stop-trigger gate, and `AutopilotSweeper`, a background task of the app), `services/reply_job_queue.py`
(the `ReplyJobQueue` Protocol), the `worker/` package (`queue.py` with `ArqReplyJobQueue`, the only
worker module the app imports; `jobs.py` with `reply_to_contact`; `composition.py`; `settings.py`
without the placeholder job), `NoticeBot` and `build_bot` in `bot/app.py`, `AUTOPILOT_NOTICES` and
the Russian texts, the reworded hand-over reason for a model-side hand-over, the optional
`AUTOPILOT_DEBOUNCE_SECONDS` setting, `autopilot_cli.py` with the `autopilot status` and
`autopilot resume` commands and their Makefile targets, the wiring in `main.py` (the pool, the
scheduler, the sweeper, the shutdown order), and the tests, with the new scans in
`tests/test_one_sender.py`.
CYR-41 added the supersession of `autopilot` rows by a newer message, so one reply goes out per burst.
CYR-40 moved the time rule of a message into `services/message_time.py` (`message_time`, used by the
intake and by `ManualMessageDetector`, which got an injectable clock), made `OutgoingMessage.sent_at`
optional like `IncomingMessage.sent_at` (a naive time is read as missing), and added
`tests/test_message_time_rule.py`. CYR-39 synced this file, the README and the guides with the code
and added `tests/test_docs_in_sync.py`, which checks the Makefile targets, the subcommands, the
migrations and the service modules against the README, `architecture.md` and this file.
CYR-42 removed every docstring and comment from `src/`, `tests/` and `migrations/` (only tool pragmas
stay), moved the two argparse descriptions that read `__doc__` into named constants, and added
`tests/test_no_comments_or_docstrings.py`, which enforces the rule.
CYR-36 added the multi-line replies: `MAX_REPLY_PARTS` and `REPLY_PART_JOINER` in `domain/reply.py`,
`services/reply_split.py` (`split_reply`), `ScheduledMessage.follows_id` and `CANCEL_REASON_PREDECESSOR`,
migration `0010` (`scheduled_messages.follows_id`, a nullable indexed foreign key to the same table), the
repository operations `enqueue_sequence`, `schedule_followers`, `cancel_orphaned_followers` and
`root_trigger_id` with the extended claim rule and the extended job-time cancellation,
`OutboxService.enqueue_sequence`, the split in the reply job (`ReplyJobResult.parts`), the follower rules
of `OutboxSender` (claim after the predecessor was sent, typing only, no availability check, the split
pause, supersession by a newer incoming message only, the orphan cancel each poll), the profile field
`split_pause_seconds` with its `schedule show` line and the sample, the new length rule of the
instructions, the numbered parts of `reply-preview`, `LimitHit` in the limiter and the limit notice with
its numbers, setting and local time (`SenderNotices.limit_time_format`), and the tests with the new scans
in `tests/test_one_sender.py`.
CYR-43 kept a lively conversation flowing: the optional settings `AUTOPILOT_DEBOUNCE_ONGOING_SECONDS`
and `AUTOPILOT_MAX_BURST_WAIT_SECONDS` with their ordering rules, the adaptive debounce of
`AutopilotScheduler` through the planner's one ongoing rule (`burst_is_ongoing`,
`HumanizationPlanner.conversation_ongoing`), the maximum wait and the effective trigger in the reply job
(`burst_messages`, `ADOPTED_NEWEST_DETAIL`, the short reaction for an adopted reply through the planner's
`ongoing` override), the sender's single supersession check before the read mark (none before the send,
none for later parts), the job-time cancellation limited to replies that have not started, and the tests,
with `tests/test_ongoing_rule.py` and the lively scenario in `tests/services/test_autopilot_lively.py`.
CYR-25 added the contact summaries: `domain/summary.py` (`ContactSummary` with its caps and tolerant
validator, `SummaryCursor`, `StoredSummary`, `read_stored_summary`, `SummaryPrompt`, `SummaryDue`,
`SummaryJobRequest`, `SummaryUpdateResult`, every summary constant), the `SummaryOutcome` and
`SummaryReason` enums, `SummaryPromptTooLarge`, migration `0011` (`contacts.summary_failed_at`),
`Contact.summary_failed_at`, the `SummaryRepository` Protocol with `SqlSummaryRepository` (messages and
count after a cursor, the compare-and-set store, the failure marker, clear, the due contacts), the
extraction texts and the summary layer in `prompts/`, `services/summary_sanitizer.py`,
`summary_updater.py` (`SummaryUpdater`, `build_summary_prompt`), `summary_sweeper.py` (`SummarySweeper`,
a background task of the app) and `summary_job_queue.py` (the one-method `SummaryJobQueue` Protocol), the
stored summary in `ReplyGenerator`, `ArqSummaryJobQueue` and `ArqReplyJobQueue.pool`, the job function
`update_contact_summary` and the updater in the worker composition, the optional `SUMMARY_ENABLED` and
`SUMMARY_EVERY_MESSAGES` settings, `summary_cli.py` with the `summary` commands and their Makefile
targets, the guide `summaries.md`, and the tests, with the new scans in `tests/test_summary_writers.py`.
CYR-44 added the early read mark: `services/early_read.py` (`EarlyReadQueue`, `EarlyReadGate`,
`EarlyReadScheduler`, none of which can reach the transport), `domain/early_read.py`
(`EarlyReadRequest`, `EARLY_READ_MIN_INTERVAL_SECONDS`), the `EarlyReadReason` and `EarlyReadOutcome` enums,
the optional `early_read` argument of `IntakeService` (asked last), the second coroutine
`OutboxSender.run_early_reads` (started by `main.py` as its own task right after the sender's and cancelled
before it; `_handle_flood_wait` and `_stop_for_account` take an optional row, `SenderNotices` gained
`flood_wait_early_read`), the optional `EARLY_READ_ENABLED` setting, the profile field `early_read_seconds`
(default `[0, 1]`, 0 to 30) with its `schedule show` line and the sample, and the tests, with the new scans in
`tests/test_one_sender.py` and `tests/test_ongoing_rule.py`.

## Detailed guides

- [architecture.md](.claude/docs/architecture.md): layers, import direction, where a file goes,
  processes, configuration, logging, health checks, testing approach.
- [contact-lifecycle.md](.claude/docs/contact-lifecycle.md): statuses, transitions, the message
  flow step by step, debounce and re-check rules.
- [safety-rules.md](.claude/docs/safety-rules.md): rules S1..S10, why, where enforced, how tested.
- [persona-and-examples.md](.claude/docs/persona-and-examples.md): the persona and style examples:
  file formats, limits, import commands, selection, privacy.
- [reply-generation.md](.claude/docs/reply-generation.md): the layered reply prompt, the budget and
  trimming, the injection defense, reply cleaning, the `reply-preview` command, what reaches the
  provider.
- [outbox-and-sender.md](.claude/docs/outbox-and-sender.md): the outbox table and states, claiming,
  the guard, crash and stale semantics, error handling, the two sources and the switch, the owner's
  commands, what is not built yet.
- [humanization.md](.claude/docs/humanization.md): the schedule profile, the planner rules with worked
  examples, the pre-send sequence, the safety net, the commands.
- [autopilot.md](.claude/docs/autopilot.md): the flow from the incoming message to the outbox row, the
  job algorithm, the outcomes and the failure policy, `reply_after`, the sweeper, the constants, the
  commands and the checklist for switching it on.
- [stop-triggers.md](.claude/docs/stop-triggers.md): the stop-trigger classes, the normalisation and the
  pattern syntax, where it is checked, the model signal, the hand-over, the notice and its buttons,
  `stop-check`, the known false positives and limits.
- [decisions.md](.claude/docs/decisions.md): what is decided and why; open questions.
- [CONTRIBUTING.md](CONTRIBUTING.md): branches, commits, Jira, pull requests, checks.

## Hard rules

1. No comments and no docstrings of any kind in source and tests: not on modules, packages
   (`__init__.py`), classes, functions or methods. Names carry the meaning; explanations go to
   markdown. Only tool pragmas (`# noqa`, `# type: ignore`, `# pragma`, `# fmt:`, `# ruff:`,
   `# mypy:`) and a shebang line are allowed. `tests/test_no_comments_or_docstrings.py` enforces it.
2. Full type hints. mypy strict passes. No `Any`.
3. Pydantic v2 at every external boundary: config, LLM responses, Telegram payloads that are
   stored, except the healthcheck entry point, which reads `HEARTBEAT_PATH` and nothing else.
4. No magic strings: model names, URLs, trigger lists and prompt texts live in config or dedicated
   modules.
5. Prompts live apart from service code.
6. Import direction as in [architecture.md](.claude/docs/architecture.md), never upward.
7. Every outgoing message goes through the sender and the status guard. No exceptions.
8. Never log message text, names or personal data at INFO or above.
9. No real personal data in git. Fixtures use fake data.
10. Smallest possible change. No side refactoring.
11. No new dependencies without agreement.
12. Never run git commands. The owner does all git work after review.
13. Anything found outside the task is not fixed in the same change. A small addition for another
    ticket is described in the report so the owner can comment on that ticket; a larger one
    becomes a separate ticket. Describe it in the report either way.

## Workflow expectations

Estimate the size before starting:

| Size | Scope | What to do |
| --- | --- | --- |
| Small | 1-2 files | Just do it |
| Medium | 3-10 files | Present the plan first |
| Large | 10+ files, or any change to a locked decision, a status transition, a safety rule or a public interface | Present the current state, the proposal, the affected files and the risks; wait for approval |

After implementing, run `make check` and `make test` and report the real results. CI runs the
same commands.

For new behaviour, derive boundary and failure cases and cover them with tests: empty input,
malformed Telegram event, invalid LLM JSON, status changed between scheduling and sending,
FloodWait. Skip tests only for docs, formatting, renames and pure config.

Keep documentation in step with code: see "What must ship in one commit" in
[CONTRIBUTING.md](CONTRIBUTING.md).
