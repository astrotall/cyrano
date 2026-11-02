# Decisions

What is decided and why, and what is still open. A decision on an open question is recorded here
in the same change that acts on it. Locked decisions are summarised in [CLAUDE.md](../../CLAUDE.md);
changing one is a large change.

## Decided

| Decision | Reason |
| --- | --- |
| One owner; the control bot answers only `OWNER_TELEGRAM_ID` | The product controls a real account; one trusted user keeps the permission model trivial |
| The owner likes in the matching bot by hand; the system never touches it | Automating a matching bot risks that account and removes the owner's judgment at the first step |
| Automatic replies only to `active` contacts, checked at scheduling and again before sending | Statuses change while a delayed job waits; a single check is not enough |
| Every outgoing message goes through the sender | One place for the guard, limits and pacing; they cannot be bypassed |
| Stop-triggers hand over: meeting or date, phone and contact exchange, money, photo requests, direct bot or AI question, low model confidence | These are the moments where a wrong answer costs the owner something real |
| Style from persona text plus few-shot examples from the owner's real chats; modes are a prompt fragment plus examples; no model training | Cheap to change, no training data pipeline, no model hosting |
| One LLM interface, DeepSeek first over plain httpx, provider replaceable by config | Avoids lock-in to an SDK; a single HTTP client is easy to fake in tests |
| PostgreSQL, async SQLAlchemy 2.0, Alembic, Redis, Arq, Docker Compose, uv | One async stack end to end; Arq needs Redis; Alembic keeps schema changes reviewable |
| Real personal data never goes to git; fixtures use fake data | The data belongs to real people |
| No message text, names or personal data in logs at INFO or above | Logs get copied and shipped; content must not travel with them |
| Python 3.14 (`requires-python >=3.14,<3.15`, `.python-version`, `python:3.14-slim-bookworm`) | One interpreter version in local runs, Docker and CI |
| Redis runs with AOF on a named volume | Arq's delayed jobs live in Redis and must survive restarts |
| Compose builds `DATABASE_URL` and `REDIS_URL` from `POSTGRES_*` and the service names | One source of truth for credentials; the same `.env` works locally and in Docker |
| `docker-compose.override.yml` publishes Postgres and Redis on `127.0.0.1` only | Host access for `psql` and a local app without exposing the services to other machines |
| `postgres:16.15-alpine` and `redis:7.4-alpine`, pinned by tag | Reproducible builds; Dependabot proposes bumps |
| Dependabot runs weekly for GitHub Actions, Docker, Docker Compose and uv | Pinned versions stay current without manual tracking |
| JSON logs to stdout, with Arq's own lines in the same format | One format for every process; the container runtime collects stdout |
| Heartbeat file for the app healthcheck; `arq --check` for the worker | The app has no HTTP server; Arq already ships a check |
| The healthcheck entry point (`cyrano.health`) reads `HEARTBEAT_PATH` from `os.environ` itself instead of `Settings`: the one exception to hard rule 3 | The module stays stdlib-only, so the check starts fast and imports nothing heavy (no pydantic, no application config); it reads that one variable and nothing else |
| The startup log line carries an allowlist of settings (`LOGGED_SETTINGS`), not everything that is not a secret | A new setting is not logged until someone adds it on purpose; Telegram ids and the session path can identify a person |
| Timestamps are `timestamptz` stored in UTC; `TIMEZONE` applied only at the edges | Avoids offset bugs in stored data |
| Migrations run in a one-shot compose service `migrate`; `app` and `worker` wait for `service_completed_successfully` | A failed migration stops the start instead of running new code on an old schema; app and worker never race each other to migrate |
| `alembic.ini` and `migrations/` live at the repository root; `env.py` is async and reads `Settings.database_url` | One driver, one source of truth for the URL, no credentials in a config file |
| `MetaData` has a naming convention | Constraint and index names are stable, so later migrations can refer to them |
| Revisions repeat table names and enum values as literals instead of importing them | A revision records the past and must not change when the code does; `alembic check` and a test catch disagreement |
| Status transitions as in [contact-lifecycle.md](contact-lifecycle.md), data plus one function in `domain/transitions.py` | One place for the rule; every other layer calls it |
| Requesting the current status is a no-op, not an error | A repeated button press or event is harmless and must not raise |
| `ignored` is terminal | The owner said no; "silence forever" must not be undone by mistake |
| A contact becomes `active` only with a mode | A reply cannot be built without a mode |
| A disabled or missing mode falls back to `default`; a missing or disabled `default` raises | Disabling a mode must not silence its contacts; when no style is available the safe result is no reply |
| Repositories are `Protocol`s in `services`, SQLAlchemy implementations in `db`, returning domain models | Services stay independent of the ORM; `db` never imports `services` |
| Every repository method is one transaction | Simple and enough for today's callers; nothing needs a wider transaction yet |
| Database tests run on real PostgreSQL, one fresh database per test, migrated by Alembic; unreachable Postgres fails the test | JSONB and native enums behave differently on anything else; a skipped test proves nothing |
| `TEST_DATABASE_URL` stays out of `Settings`; tests read it themselves from the environment or `.env` | The application must not know about a test database or require the variable in production |
| Dependencies added for storage: `sqlalchemy[asyncio]`, `asyncpg`, `alembic` | Agreed for CYR-9; together they are the stack already decided above |
| Dependency added for the userbot: `telethon` (1.45.0 at the time of CYR-10), imported only inside `userbot/`, typed through Protocols with a per-module mypy override (`ignore_missing_imports`) | Agreed for CYR-10; it ships no type information, and the Protocols keep our code free of `Any` and testable with fakes |
| `NewContactEvent` is best-effort; the database is the source of truth | A lost event must not lose a contact. Contacts in status `new` are in the database whether or not their event was delivered, so CYR-12 can always find them |
| A bounded in-memory queue (1000) carries the events; a full queue drops the new event and logs a warning with counters | Intake must never block or fail because a consumer is slow or missing. Nothing consumes the queue yet (CYR-12) |
| A contact is registered on any private message from a real user, media-only messages included; the text is the caption or an empty string; media is not downloaded | The owner has to see everyone who wrote. `messages.text` allows an empty string, so no placeholder is needed. Downloads belong to a later ticket |
| NUL bytes are removed from text, username and display name before any repository call | PostgreSQL rejects NUL in text and JSONB; otherwise one such message would fail every redelivery of itself |
| Two simultaneous first messages are resolved by get-after-conflict: the loser of `create` reads the contact by `tg_id` and continues as for a known sender | The unique `tg_id` already arbitrates; no extra lock or retry loop. Exactly one call created the contact, so exactly one event is published |
| Catch-up on every start: the client is built with `catch_up=True` and `catch_up()` is called after connect | Messages that arrived while the app was down are processed. Calling `catch_up()` alone would not load the update state saved in the session. Idempotency makes replays safe |
| The handler is registered before the client connects | No update can arrive at a client without a handler |
| The app exits at once with "run `make login`" when the session file is missing, before any client exists; an unauthorized session and a 30 s connect timeout are readable fatal errors too; normal startup never prompts | A restart loop must never hang, prompt for input or touch the network without a session. The restart policy handles the rest |
| An unexpected disconnect ends the process with an error | Telethon already retries; when it gives up, a restart is the honest recovery, and the restart policy provides it |
| The handler retries the intake call: 3 attempts in total, 1 s then 2 s apart, for any exception except `asyncio.CancelledError`; after the last attempt it logs an error with the class name and counters and drops the message | A transient failure (a database blip) should not lose a message. Intake is idempotent (message append on `(contact_id, tg_message_id)`, contact creation by get-after-conflict), so a repeat is safe. Updates are sequential, so a retry delays the next update by a few seconds; accepted. Limit: a failure after the contact was created but before its event was published still loses the event (events are best-effort) |
| SIGTERM and SIGINT during startup end it at once: the connect and the catch-up are each raced against the stop event, the 30 s connect timeout stays for the real timeout case | The connect timeout is longer than Docker's 10 s grace period, so waiting it out meant SIGKILL. A stop request must give a clean shutdown (handler removed, client closed, engine disposed, exit 0) in about a second |
| The session is created by `python -m cyrano login` (`make login`, `make login-docker`); phone, code and password are asked, passed on and never stored or logged; the phone is asked before any network call; the file gets mode `0600` | The session file is a secret that controls the second account. Asking first lets the owner abort without any network traffic |

## Open questions

| Question | Ticket |
| --- | --- |
| How does the worker send through the userbot session? The session volume is mounted into `app` only | CYR-17 |
| How does the worker reconnect to Redis after a Redis restart? | CYR-17 |
| Scrubbing of free-text logs and coverage tooling | CYR-28 |
| Explicit compose project name (today it defaults to the directory name) | open, no ticket |
| Debounce mechanism and window length | CYR-17 |
| How many last messages go into the prompt | CYR-16 |
| Where the layered prompt builder lives (`llm`, `services` or `prompts`) | CYR-16 |
| How stop-triggers are detected (lists, model output, both), where each is evaluated, and how model confidence is obtained and thresholded | CYR-20 |
| Numbers: rate limits, humanized delay, quiet hours window | CYR-22, CYR-18 |
| Numbers: limits on proactive messages beyond two unanswered | CYR-23 |
| May `bot`, `userbot` and `worker` import each other, or only `services`? | open, no ticket |
| Order of the consumers of `NewContactEvent`: profile collection (CYR-11) before the card (CYR-12)? | CYR-11, CYR-12 |
| Which hooks CYR-29 installs and which tool manages them | CYR-29 |
