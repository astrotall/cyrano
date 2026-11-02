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

The skeleton (CYR-8), the storage layer (CYR-9), the userbot intake (CYR-10), the profile
collection (CYR-11) and the control bot with the contact card (CYR-12) exist. Everything
else is planned and marked with its ticket key.
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
| Hand-over | Meeting or date proposals, phone and contact exchange, money, photo requests, a direct question whether it is a bot or AI, and low model confidence stop the conversation and notify the owner | The assistant never answers those itself |
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
| Arq, Redis 7.4 | in the project (skeleton worker, placeholder job) |
| PostgreSQL 16.15 | in the project (compose, migrations, tests) |
| SQLAlchemy 2.x async, asyncpg, Alembic | in the project (schema, repositories, used by the intake) |
| Ruff, mypy strict, pytest, pytest-asyncio | in the project |
| Docker, Docker Compose, GitHub Actions, Dependabot | in the project |
| Telethon | in the project (userbot listens, registers contacts and reads profiles; imported only in `userbot/`) |
| aiogram 3 | in the project (control bot; imported only in `bot/`) |
| DeepSeek client over plain httpx | planned, CYR-13 |
| respx | planned, with CYR-13 |

## Repository layout

```
.
├── CLAUDE.md, CONTRIBUTING.md, README.md
├── pyproject.toml, uv.lock, .python-version
├── Makefile                      includes login and login-docker
├── Dockerfile, docker-compose.yml, docker-compose.override.yml
├── .env.example, .dockerignore, .gitignore
├── alembic.ini
├── migrations/                   async env.py, versions/
├── .claude/docs/                 architecture, contact-lifecycle, safety-rules, decisions
├── .github/                      dependabot.yml, workflows/ci.yml
├── src/cyrano/
│   ├── __main__.py, main.py      entrypoint (composition root), login subcommand, signals, heartbeat
│   ├── config.py                 Settings, load_settings, ConfigError
│   ├── logging.py                JSON-lines logging
│   ├── health.py                 heartbeat file and the app healthcheck
│   ├── domain/                   enums, status transition rules, errors, Pydantic models
│   ├── db/                       SQLAlchemy models, engine and sessions, repositories
│   ├── services/                 repositories (Protocols), intake, sanitize, contact_events, profile_source,
│   │                             leomatch, profile_collector, card_sender, card_builder, card_announcer,
│   │                             card_actions; the rest is planned
│   ├── userbot/                  Telethon: protocols, filters, handler, client, login, runner, errors,
│   │                             profile_source, leomatch_source, leomatch_match
│   ├── bot/                      aiogram: app, middleware, handlers, rendering, callbacks, sender, messages, errors
│   ├── llm/                      empty skeleton package
│   └── worker/settings.py        Arq WorkerSettings, placeholder job
└── tests/                        mirrors src/cyrano
```

CYR-8 added the package skeleton, typed configuration, JSON logging, app process with heartbeat,
Arq worker skeleton, Docker Compose, CI and Dependabot. CYR-9 added `domain/`, the content of
`db/`, `services/repositories.py`, `alembic.ini`, `migrations/`, the one-shot `migrate` compose
service and the database tests. CYR-10 added `userbot/` (the only package that imports Telethon;
it listens and registers, it never sends), `services/intake.py`, `services/sanitize.py`,
`services/contact_events.py`, the `IncomingMessage` and `NewContactEvent` models, the
`python -m cyrano login` command with the `make login` and `make login-docker` targets, and the
userbot tests with their fakes. CYR-11 added the profile collection: `services/profile_collector.py`
(`ProfileCollector`, started by `main.run` next to the userbot), `services/profile_source.py` (the
`ProfileSource` and `LeomatchSource` Protocols, `ProfileSourceError`), `services/leomatch.py` (the
card parser), the read-only Telethon adapters `userbot/profile_source.py`,
`userbot/leomatch_source.py` and `userbot/leomatch_match.py`, the `ContactProfile`,
`LeomatchProfile`, `TelegramProfile`, `LeomatchSourceText` and `ContactReadyEvent` models,
`ContactRepository.update_identity`, the generic in-memory event queue (two instances), the
`LEOMATCH_BOT_USERNAME` setting, and the tests with their fakes. CYR-12 added the `aiogram`
dependency and the `bot/` package (the only one that imports aiogram: `ControlBot` with the polling,
the owner-only middleware, the handlers, the card rendering and keyboard, `TelegramCardSender`, the
Russian texts in `messages.py`, the readable fatal errors), `services/card_sender.py` (the
`CardSender` Protocol), `services/card_builder.py`, `services/card_announcer.py` (`CardAnnouncer`,
started by `main.run` next to the control bot), `services/card_actions.py` (`CardActions`), the
`ContactCard`, `CardLeomatch`, `ModeChoice`, `CardActionResult` models and the `CardOutcome` enum,
and the tests with their fakes (a recording aiogram session, `FakeControlBot`).

## Detailed guides

- [architecture.md](.claude/docs/architecture.md): layers, import direction, where a file goes,
  processes, configuration, logging, health checks, testing approach.
- [contact-lifecycle.md](.claude/docs/contact-lifecycle.md): statuses, transitions, the message
  flow step by step, debounce and re-check rules.
- [safety-rules.md](.claude/docs/safety-rules.md): rules S1..S10, why, where enforced, how tested.
- [decisions.md](.claude/docs/decisions.md): what is decided and why; open questions.
- [CONTRIBUTING.md](CONTRIBUTING.md): branches, commits, Jira, pull requests, checks.

## Hard rules

1. No explanatory comments in source code. Names carry the meaning; explanations go to markdown.
   One-line package docstrings are fine.
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
