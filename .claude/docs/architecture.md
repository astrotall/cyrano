# Architecture

State as of CYR-11: the skeleton, the storage layer, the userbot intake and the profile collection exist. Anything
else is marked **planned** with its ticket.
Product and decisions: [CLAUDE.md](../../CLAUDE.md). Reasons: [decisions.md](decisions.md).

## Layers

Source lives in `src/cyrano/`.

| Package or module | Role | State |
| --- | --- | --- |
| `userbot/` | The only package that imports Telethon: client, session and login, event filters, the incoming-message handler, the listener lifecycle. Receives private messages on the second account and converts events into plain objects. Also holds the read-only adapters of the profile collection: `profile_source.py` (bio, names, photo) and `leomatch_source.py` with `leomatch_match.py` (the matching-bot chat). Planned: the actual Telegram send behind the sender (CYR-19) | exists for listening and registration (CYR-10) and for reading profiles (CYR-11); it sends nothing |
| `bot/` | aiogram control bot: owner-only, contact card, Reply/Ignore buttons, notifications | empty skeleton; planned CYR-12 |
| `llm/` | LLM client: DeepSeek over plain httpx, response parsing | empty skeleton; planned CYR-13 |
| `db/` | SQLAlchemy models (`models.py`), engine and session factory (`session.py`), repositories (`repositories.py`) | exists; called by the intake (CYR-10) |
| `services/` | Business logic shared by userbot, bot and worker: contact lifecycle, sender, reply pipeline. `repositories.py` holds the repository Protocols, `intake.py` the `IntakeService`, `sanitize.py` the NUL-byte removal, `contact_events.py` the generic `ContactEventPublisher[T]` and `ContactEventSubscriber[T]` Protocols and the in-memory queue; CYR-11 added `profile_source.py` (the `ProfileSource` and `LeomatchSource` Protocols and `ProfileSourceError`), `leomatch.py` (the card parser) and `profile_collector.py` (`ProfileCollector`) | Protocols, intake, sanitization, events (CYR-10) and the profile collector (CYR-11) exist; the rest is planned from CYR-12 on |
| `worker/` | Arq worker: `settings.py` holds `WorkerSettings`, `startup`, `shutdown`, a placeholder `noop` job (Arq refuses to start a worker with no registered functions) | skeleton works; real jobs planned CYR-17 |
| `config.py` | `Settings` (pydantic-settings), `load_settings`, `ConfigError` | exists |
| `logging.py` | JSON-lines formatter, `build_log_config`, `configure_logging` | exists |
| `health.py` | Heartbeat file, `is_alive`, `python -m cyrano.health` | exists |
| `main.py`, `__main__.py` | App entrypoint (composition root), the `login` subcommand, signal handling, heartbeat task | exists; the userbot is plugged in (CYR-10), the bot by CYR-12 |
| `domain/` | Leaf package, imports only pydantic and the standard library: enums, status transition rules, domain errors, Pydantic models the layers exchange | exists |
| `prompts` | Prompt texts, few-shot data loading, trigger lists | planned, not created; the layered builder's location is decided in CYR-16 |

`tests/` mirrors the packages (`tests/userbot`, `tests/bot`, `tests/llm`, `tests/domain`,
`tests/db`, `tests/services`, `tests/worker`); the skeleton packages have empty test packages.
`tests/userbot/fakes.py` holds the fake Telethon client, event and sender.

## Import direction

Enforced by review. `tests/test_import_direction.py` also checks the part that exists: `domain`
imports only pydantic, the standard library and itself; `db` imports neither `services` nor `llm`;
`services` imports neither `db`, `userbot` nor `bot`; `userbot` imports none of `db`, `llm`, `bot`;
and only the `userbot` package imports `telethon`.

1. `bot`, `userbot` and `worker` may import `services`.
2. `services` reach `llm` and `db` only through interfaces defined in `services`. The concrete
   `llm` and `db` code satisfies those interfaces without importing `services`. For `db` this is
   fact: `services/repositories.py` defines `ContactRepository`, `MessageRepository` and
   `ModeRepository`, `db/repositories.py` satisfies them structurally, and mypy checks that in
   `tests/db/test_session.py`.
3. `llm` and `db` never import `services` or each other.
4. `config`, `domain` and `prompts` are leaves: they import no application layer. Any layer may
   import them. Today `config` also imports `health` for the default heartbeat path (`health` is
   stdlib-only), so `config` is not a pure leaf until that default moves.
5. `main` is the composition root: the only place that builds concrete objects and passes them
   to the layers.
6. Telethon is imported only inside `userbot/`. The rest of the code, tests of other layers
   included, sees it through the Protocols in `userbot/protocols.py`.

Never import upward. Whether `bot`, `userbot` and `worker` may import each other is an open
question in [decisions.md](decisions.md).

## Where does this file go?

| You are writing | It goes to |
| --- | --- |
| A rule about contacts, statuses, when to reply, the sender and its guard | `services` |
| Telethon events, sessions, converting a Telethon entity into a plain object | `userbot` |
| aiogram handlers, keyboards, card formatting | `bot` |
| HTTP calls to the LLM provider and parsing its response | `llm` |
| SQLAlchemy models, repository implementations, session handling | `db` |
| A repository interface | `services/repositories.py` |
| Arq job functions and worker settings | `worker` |
| A data type or rule used by several layers | `domain` |
| Prompt text, few-shot loading, trigger lists | `prompts` (planned) or config |
| An environment-driven setting | `config.py` and the README Settings section, together; `.env.example` too when the variable is required |
| Wiring concrete objects together | `main.py` |
| Alembic migrations | `migrations/versions/` at the repository root |

## Process model

Two Python processes, two stores, one external API each for Telegram and the LLM.

```
 Telegram <--> app process (python -m cyrano)       worker process (arq)
               userbot (exists) + control bot (planned)   delayed jobs (planned)
               heartbeat task (exists)               placeholder job (exists)
                  |        |                            |        |
                  v        v                            v        v
              PostgreSQL  Redis  <---- Arq jobs ----->  Redis  PostgreSQL
                                                          |
                                                          v
                                                     LLM (DeepSeek)
```

- **app**: `python -m cyrano`. It loads config, starts JSON logging and requires the session file
  (see [Userbot](#userbot-cyr-10)). Then it builds the database engine (which opens no connection
  until something uses it), the repositories, the `IntakeService` and the two in-memory event queues,
  touches the heartbeat file, connects the userbot, registers the handler, catches up, starts the
  profile collector as a background task (only once the userbot is connected, because it reads
  through the same client) and waits for SIGTERM or SIGINT; a signal during connect or catch-up ends the start at once (see
  [Userbot](#userbot-cyr-10)). A lost connection that Telethon cannot restore ends the process with an
  error, and the restart policy starts it again. Planned: the control bot runs on the same event
  loop (CYR-12). On shutdown it cancels the collector, removes the handler, disconnects the userbot, stops the heartbeat,
  removes the file and disposes the engine; planned: stop the control bot and close the Redis
  connections there. It has the `userbot_session` volume.
- **login**: `python -m cyrano login`, run on demand (`make login`, `make login-docker`). It is the
  same image and the same `Settings` as the app, shares the `userbot_session` volume and needs no
  database.
- **worker**: `arq cyrano.worker.settings.WorkerSettings`. Arq imports `settings.py` itself, so a
  missing or invalid configuration ends the import with `SystemExit` and the readable `ConfigError`
  text instead of a traceback chain. Planned: runs the delayed reply job
  (CYR-17) and background jobs (CYR-23, CYR-24, CYR-25). It has the `media` volume but not the
  session volume; how it sends through the userbot session is an open question (CYR-17).
- **migrate**: one-shot, `alembic upgrade head`, then exits. Same Dockerfile as app and worker, same
  environment, `restart: "no"`. Depends only on healthy postgres. It exits non-zero with the
  readable `ConfigError` text if the settings are invalid.
- **postgres**: `postgres:16.15-alpine`, volume `postgres_data`.
- **redis**: `redis:7.4-alpine` with AOF on `redis_data`. Arq stores delayed jobs here.
- The Dockerfile builds one image, tagged `cyrano:local`, for app, worker and migrate (Python 3.14
  slim, uv only in the builder stage, non-root user `cyrano`, uid 10001). It includes `alembic.ini`
  and `migrations/`. Compose overrides the command per service.
- `depends_on` waits for healthy postgres and redis and for `migrate` to exit with 0
  (`service_completed_successfully`) before app and worker start.

### Profile collection (CYR-11)

People arrive from a matching bot. The owner's chat with that bot (Leomatch) holds the person's
card and the match message; the collector reads them together with nick, name, bio and photo for
the owner's card. The system never sends anything to the matching bot. The steps, the stored
structure and the semantics of `collected_at` are in
[contact-lifecycle.md](contact-lifecycle.md#profile-collection-cyr-11).

## Userbot (CYR-10)

`src/cyrano/userbot/`. The one package that imports Telethon. It only listens and stores: no
module calls `send_*`, `mark_read`, `send_read_acknowledge` or any other write method.

| Module | Role |
| --- | --- |
| `protocols.py` | The small surface of Telethon that the code uses, as Protocols: `SenderLike`, `IncomingEvent`, `UserbotClient`, `LoginClient`. `UserbotClient` has `connect`, `disconnect`, `is_user_authorized`, `catch_up`, `add_event_handler`, `remove_event_handler`, `disconnected` and, since CYR-11, only the read members `get_entity`, `download_profile_photo`, `iter_messages` and `__call__` (typed to accept only the full-user request); no write method; a test enforces that list |
| `filters.py` | Pure functions: `event_rejection(event)` and `sender_rejection(sender)` return a `Rejection` or `None` |
| `handler.py` | `IncomingHandler`: registers one `events.NewMessage(incoming=True)` handler, converts an event into `IncomingMessage`, calls the intake, catches and counts failures |
| `client.py` | Builds the `TelegramClient` from `Settings`, session file naming and mode, `connect_authorized` with the timeout, `disconnect_client` |
| `runner.py` | `Userbot`: start, run until stopped or disconnected, stop |
| `login.py` | The interactive login flow with injected prompts |
| `errors.py` | `UserbotError` and subclasses; their messages are meant to be read by the owner |
| `profile_source.py` | `TelethonProfileSource`, the `ProfileSource` adapter: the user from the session cache (`get_entity`), the bio from `GetFullUserRequest`, the big current profile photo. Every Telethon failure becomes `ProfileSourceError` carrying the class name; it never sleeps on a flood wait |
| `leomatch_source.py` | `TelethonLeomatchSource`, the `LeomatchSource` adapter: resolves the chat with `LEOMATCH_BOT_USERNAME` (an unresolvable chat gives an empty list and a DEBUG line), searches, scans, walks back for the card; `to_chat_message` turns Telethon messages and entities into the plain `ChatMessage` |
| `leomatch_match.py` | Pure: `ChatMessage`, `match_evidence` (the strict match check), `looks_like_match_message`, `normalize_username` |

- **Why Protocols.** Telethon ships no type information. The Protocols keep mypy strict on our code
  without `Any`, let tests use plain fakes, and make "the userbot never sends" structural: the
  client Protocol has no write method, so code typed against it cannot call one. mypy only has a
  per-module override for `telethon` (`ignore_missing_imports`, in `pyproject.toml`).
- **Two filter functions, not one.** `event_rejection` uses only what the event carries, so
  groups and channels are dropped without a network call; `sender_rejection` needs the sender,
  which the handler fetches with `get_sender()` (a read request, cached in most cases).
- **Handler.** An event goes through both filters, then becomes an `IncomingMessage`: `tg_id`,
  `username` (missing means `None`), `display_name` (first and last name, missing means `None`),
  `tg_message_id`, `text` (caption or empty), `sent_at` (UTC). The handler catches every
  `Exception` from one message and logs only the class name and counters. The intake call is
  retried: 3 attempts in total, with the delays in `INTAKE_RETRY_DELAYS_SECONDS` (1 s, then 2 s)
  between them, for any `Exception`; `asyncio.CancelledError` is never retried. Only the intake call
  is retried, not the conversion of the event. Intake is idempotent, so a repeated attempt is safe.
  Each failed attempt but the last is a WARNING with the error class and the attempt number; after
  the last one the handler logs the ERROR and drops the message. Updates are handled one after
  another (`sequential_updates`), so a retry delays the next update by up to 3 s. The sleep is a
  constructor parameter so tests do not wait.
- **Client settings.** `catch_up=True` (Telethon then loads the saved update state at connect),
  `sequential_updates=True` (updates are handled in order), bounded retries and request timeout, all
  named constants in `client.py`. The parent directory of `USERBOT_SESSION_PATH` is created before
  the client is built; Telethon does not create it.
- **Session file.** Telethon appends `.session` to `USERBOT_SESSION_PATH`; `session_file()` mirrors
  that. The file is a secret: whoever has it controls the second account. `login` creates it with
  umask `077` and sets mode `0600` after a successful login. It is git-ignored and
  docker-ignored.
- **Startup** (in this order, in `main.run`): the session file must exist, otherwise the app exits
  with code 1 and the message "run `make login`" before any client exists, so before any network
  call. Then the handler is registered (before connecting, so no update can arrive without a
  handler), the client connects within `CONNECT_TIMEOUT_SECONDS` (30 s; a timeout and a refused
  connection are readable fatal errors), a session that is not authorized is the same kind of
  error, and then `catch_up()` is called. Normal startup never prompts for input.
- **Signals during startup.** `Userbot.start(stop)` races the connect and the catch-up, each
  separately, against the stop event that SIGTERM and SIGINT set (`_until_stopped` in `runner.py`).
  The signal handlers are installed before the client is built, so a signal at any point of the
  start counts. On a signal the pending call is cancelled, `start` returns without the "listening"
  line, `run_until` returns at once because the event is set, and the normal shutdown runs: remove
  the handler, disconnect (bounded), stop the heartbeat, dispose the engine, exit 0 without a
  traceback. This matters because `CONNECT_TIMEOUT_SECONDS` (30 s) is longer than Docker's 10 s
  grace period. The 30 s timeout stays for the case where no signal comes, and still ends the
  start with the readable `ConnectTimedOut` error.
- **Run and shutdown.** The app waits for SIGTERM/SIGINT or for the client's `disconnected` future.
  A disconnect nobody asked for is an error exit. Shutdown removes the handler, disconnects (bounded
  by a timeout) and only then lets the engine be disposed.
- **Login command.** `python -m cyrano login` asks for the phone before anything touches the
  network, then connects, requests the code, signs in and asks for the two-step password only if
  Telegram demands it (`getpass`). Phone, code and password are passed on and never stored, logged
  or put in `Settings`; on success it prints only that the session was saved. A failed login
  removes a session file that it created. Prompts and the client factory are parameters, so tests
  run it without a terminal or a network.
- **Read-only profile access (CYR-11).** `tests/userbot/test_never_sends.py` also pins the client members above, scans the userbot modules for imported Telethon request classes (only `GetFullUserRequest` is allowed) and runs the adapters against fakes whose write methods record and raise.
- **Logging.** No message text, names, usernames, phone numbers or Telegram ids at INFO or above.
  Rejections are DEBUG with a reason and a counter. Handler failures are ERROR with the class name and
  counters. `telethon` follows `TELETHON_LOG_LEVEL`.

## Intake and contact events (CYR-10)

`services/intake.py`: `IntakeService(contacts, messages, events).handle(incoming)` returns an
`IntakeResult` (contact id, message id, whether this call created the contact). It depends only on
the repository Protocols and on `ContactEventPublisher`. The steps, the race handling and the
idempotency are in [contact-lifecycle.md](contact-lifecycle.md#registration-cyr-10).

`services/sanitize.py`: `strip_nul` and `sanitize_incoming`, pure.

`services/contact_events.py`: the generic `ContactEventPublisher[T]` Protocol (`async publish`),
`ContactEventSubscriber[T]` (`subscribe()`) and `InMemoryContactEvents[T]` on a bounded
`asyncio.Queue` (`EVENT_QUEUE_SIZE`, 1000) with an async `subscribe()` iterator. `main.run` builds
two instances: `NewContactEvent` (published by the intake, consumed by the profile collector) and
`ContactReadyEvent` (published by the collector, consumed by the card in CYR-12; nothing consumes
it yet). A full queue drops the new event and logs
a warning with counters; `publish` never raises. The queue lives in one process and is lost on
restart, which is fine because events are best-effort and the database is the source of truth.

## Profile collection (CYR-11)

`services/profile_collector.py`: `ProfileCollector(contacts, profiles, leomatch, new_contacts,
ready, media_dir)`. It depends on the `ContactRepository` Protocol, on the two source Protocols in
`services/profile_source.py` and on the event Protocols, never on Telethon or SQLAlchemy.

- **Protocols.** `ProfileSource.fetch_profile(tg_id)` returns a `TelegramProfile` (username,
  display name, bio); `ProfileSource.download_photo(tg_id, destination)` returns whether a photo was
  saved; `LeomatchSource.find_texts(tg_id, username)` returns a list of `LeomatchSourceText` labelled
  `match` or `card` (empty when nothing is found). Every adapter failure (flood wait, privacy
  restriction, not found, timeout) is one `ProfileSourceError`.
- **Parser.** `services/leomatch.py`: `parse_leomatch_text` never raises and fills name, age, city
  and about only for the assumed card format; `build_leomatch_profile` assembles the stored
  `LeomatchProfile` from the labelled texts. Linear time: it splits strings and uses one simple
  pattern, no nested quantifiers.
- **Process.** Started by `main.run` after the userbot connected and caught up, cancelled first on
  shutdown, before the client disconnects. A collector that crashes is logged once (class name) and
  does not stop the intake.
- **Media directory.** `MEDIA_DIR/profiles/<internal contact id>.jpg`, created on demand;
  `<id>.part.jpg` exists only during a download. The `media` compose volume is mounted into `app`
  and `worker`.
- **Logger.** `cyrano.services.profile_collector` (INFO: contact id and which parts produced data;
  WARNING: contact id, part name and error class of a failed part; ERROR: error class and counter
  of a failed event), `cyrano.userbot.leomatch_source` (DEBUG only).

## Persistence

- **Tables**: `contacts`, `messages`, `modes`; native enums `contact_status` and
  `message_direction`. Columns and rules: [contact-lifecycle.md](contact-lifecycle.md).
- **Names**: table names and enum type names live in `db/models.py`, enum values in
  `domain/enums.py`. `Base.metadata` has a naming convention, so constraint and index names are
  stable and need no spelling out. The migration repeats these strings on purpose: a revision is a
  frozen record of the past, so it must not change when the code does. `alembic check` and
  `tests/db/test_migrations.py` prove the two agree.
- **Time**: every timestamp is `timestamptz`; the domain models reject naive values and normalize
  to UTC. `touch_last_message_at` refuses a naive `datetime`.
- **Repositories**: take a session factory. Every method is one transaction, returns domain models
  and never an ORM object, and turns constraint violations into domain errors with `from None`, so
  the driver's message (which names key values) does not travel. Writes that read first lock the
  row with `FOR UPDATE`. Nothing spans several repository calls yet; when a later ticket needs that
  to be atomic, it changes this rule here.
- **Engine and sessions**: `build_engine(settings)` and `build_session_factory(engine)`. Nothing is
  built at import time; the composition root owns the lifecycle. The worker does not build an engine
  yet (CYR-17).
- **Migrations**: `alembic.ini` holds no URL. The async `migrations/env.py` reads
  `Settings.database_url`, so there is one source of truth, and logs through `configure_logging`.
  `--sql` prints plain SQL and configures no logging, so log lines do not end up in the output. The
  autogenerate comparison includes column types and server defaults. It does not see enum values,
  so a test compares them with `domain/enums.py`.
- **Default mode**: migration `0001` seeds `default` with a neutral placeholder fragment and no
  examples, idempotently. The real modes belong to CYR-15.

## Configuration

`cyrano.config.Settings` is a frozen pydantic-settings model. A field `foo_bar` is read from
`FOO_BAR`; empty values count as unset; unknown variables are ignored. `load_settings()` reads
`.env` from the current directory (real environment variables win) and raises `ConfigError`
listing every missing or invalid variable at once, without echoing values: validator messages
never contain the value because URLs may carry passwords, and the readable error is built without
the raw input. `load_settings(env_file=None)` ignores any `.env`.

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `TELEGRAM_API_ID` | yes | | int |
| `TELEGRAM_API_HASH` | yes | | `SecretStr` |
| `USERBOT_SESSION_PATH` | yes | | path |
| `BOT_TOKEN` | yes | | `SecretStr` |
| `OWNER_TELEGRAM_ID` | yes | | int |
| `DEEPSEEK_API_KEY` | yes | | `SecretStr` |
| `DATABASE_URL` | yes | | `SecretStr`; must start with `postgresql+asyncpg://` |
| `REDIS_URL` | yes | | `SecretStr`; `redis://` or `rediss://` |
| `LOG_LEVEL` | no | `INFO` | case-insensitive |
| `TIMEZONE` | no | `Europe/Moscow` | validated as an IANA name |
| `MEDIA_DIR` | no | `media` | path |
| `DEEPSEEK_MODEL` | no | `deepseek-chat` | |
| `TELETHON_LOG_LEVEL` | no | `WARNING` | |
| `AIOGRAM_LOG_LEVEL` | no | `WARNING` | |
| `HTTPX_LOG_LEVEL` | no | `WARNING` | also applied to `httpcore` |
| `LEOMATCH_BOT_USERNAME` | no | `leomatchbot` | username of the matching bot whose chat is searched for a contact's texts; a leading `@` is removed; the default is an assumption to check in Telegram; not in the startup log line |
| `HEARTBEAT_PATH` | no | `<tmpdir>/cyrano-heartbeat` | liveness file; also read straight from `os.environ` by `cyrano.health`, the one exception to hard rule 3 ([decisions.md](decisions.md)) |

`TEST_DATABASE_URL` is not in this table: tests read it themselves
([decisions.md](decisions.md)).

- **Secrets** are `SecretStr`: `repr`, `str` and dumps mask them; `.get_secret_value()` is called
  only where the value is used. Settings that carry URLs with credentials are secrets too.
- **Local vs Docker** (CYR-8): `.env.example` is a bare template without comments: required
  variables and local values (`DATABASE_URL` and `REDIS_URL` on `localhost`, relative
  `USERBOT_SESSION_PATH` and `MEDIA_DIR`), `POSTGRES_*` and `TEST_DATABASE_URL`. The README describes every variable and
  the two run modes. Compose builds `DATABASE_URL` and `REDIS_URL` for app and worker from
  `POSTGRES_*` and the service names and sets `USERBOT_SESSION_PATH=/data/session/userbot` and
  `MEDIA_DIR=/data/media`; `environment:` beats `env_file:`, so the localhost values are ignored
  in containers.
- **Time**: timestamps are stored in UTC; `TIMEZONE` is applied only at the edges (planned
  users: quiet hours in CYR-18 and times shown to the owner). `Settings.tzinfo` returns the `ZoneInfo`. `tzdata` is a
  dependency because the slim image has no system zone database.
- **New setting**: add the field to `Settings` and describe the variable in the README Settings
  section in the same commit; a required variable also goes to `.env.example`.

## Logging

- JSON lines on stdout: `ts` (UTC, ISO 8601, milliseconds), `level`, `logger`, `msg`, then any
  `extra=` fields, and `exc` for exceptions.
- App and worker share the format. The worker passes `LOG_CONFIG` to `arq --custom-log-dict`, so
  Arq's own lines, including those before `startup`, use the same formatter.
- Context goes through `extra=`: every `LogRecord` attribute outside the stdlib set becomes a field.
  Everything propagates to the root handler, so libraries that log through the stdlib (Arq
  included) are rendered by the same formatter.
- The userbot loggers are `cyrano.userbot`, `cyrano.userbot.handler` and
  `cyrano.userbot.leomatch_source`, intake is `cyrano.services.intake`, events
  `cyrano.services.contact_events`, the collector `cyrano.services.profile_collector`.
- Library loggers (`telethon`, `aiogram`, `httpx`, `httpcore`) have their own levels
  (`*_LOG_LEVEL`, default `WARNING`), independent of `LOG_LEVEL`. `httpcore` follows
  `HTTPX_LOG_LEVEL` because it is httpx's transport.
- A `SecretStr` passed in `extra=` is masked as a second line of defence.
- **Startup line**: the app and the worker log `Settings.safe_summary()`, which returns only the
  settings listed in `LOGGED_SETTINGS` (log levels, timezone, media dir, model name, heartbeat
  path). No secret, no `OWNER_TELEGRAM_ID`, no `TELEGRAM_API_ID`, no session path. A new setting is
  withheld until it is added to that list.
- The worker passes the same config dict to `arq --custom-log-dict`, so Arq never installs its own
  plain formatter. `startup` calls `configure_logging` again, which is idempotent, so JSON logging
  also holds when the worker is started without that flag.
- **No personal data at INFO or above**: no message text, names, bios, profile text. Log internal
  ids, statuses, counts, durations; Telegram ids are personal data too. This is a review rule
  today; scrubbing of free-text logs is an open question (CYR-28).
- **Database**: repositories log nothing. The engine sets `hide_parameters=True`, so a driver error
  or an enabled `sqlalchemy.engine` logger never shows bound values such as message text. Domain
  errors carry internal ids and statuses, never a Telegram id.
- `migrate` logs through the same formatter, so Alembic's lines are JSON too.

## Health checks

- **app**: no HTTP server. `heartbeat_loop` on the app's event loop touches `HEARTBEAT_PATH` every
  10 s; the compose healthcheck `python -m cyrano.health` exits 1 if the file is missing or older
  than 60 s. A blocked or dead loop stops the heartbeat, so the container turns unhealthy. The
  file is removed on shutdown, so a stopped app does not look alive. `cyrano.health` is
  stdlib-only so the check starts fast and needs no application config; it reads `HEARTBEAT_PATH`
  itself (exception to hard rule 3, see [decisions.md](decisions.md)).
- **worker**: `arq --check cyrano.worker.settings.WorkerSettings`. Arq refreshes its health key
  in Redis every `health_check_interval` (60 s, Arq's default is one hour; the key expires one
  second later), which `arq --check` reads.
- **postgres**: `pg_isready`. **redis**: `redis-cli ping`.

## Testing approach

- pytest with `asyncio_mode = auto`; mypy strict also covers `tests/`.
- Every test starts from a clean environment (autouse fixture removes all Cyrano variables) and
  uses fake values only. An autouse fixture restores the root and library loggers after each test.
  Tests that load settings pass `env_file=None` or change into `/` so no stray `.env` is read.
- Assertions on startup output cover secrets and personal identifiers (owner id, API id).
- Exists: config loading and validation, secret masking, logging format, heartbeat and
  healthcheck, the app loop, a real subprocess that must exit cleanly on SIGTERM (it runs
  `tests/fake_app.py`, which wires a fake Telethon client into the real `run`), a real subprocess
  that must exit with the "run make login" message when the session file is missing, worker settings,
  status transition rules (pure, no database), domain models, migrations and repositories.
  CYR-10 added: NUL sanitization, the event queue, the intake on real PostgreSQL (including two
  concurrent first messages), the filters, the handler with fake events (including the retry:
  one failure then success, always failing, cancellation not retried), the client and startup
  logic with a fake client (including a connect or catch-up that never finishes plus a stop
  signal, and a real subprocess that must exit within 2 s of SIGTERM while connecting), the login flow with injected prompts, and the structural test that
  the userbot never sends (`tests/userbot/test_never_sends.py`).
  CYR-11 added: the card parser (formats, boundaries, text that would break a naive regex), the
  strict match check, the lookup in the matching-bot chat against a fake client with real Telethon
  entity objects (search hit and miss, both chain shapes, the walk-back rules), the profile adapter
  with Telethon errors, the collector with fake sources (every part failing alone and together, the
  total timeout, the sweep, order, logs without personal data), `update_identity` and the profile
  JSONB round trip on real PostgreSQL, and in `tests/test_main.py` the collector task being
  started, cancelled before the disconnect and unable to stop the intake. Those tests replace the
  repositories in `main` with in-memory ones, so none of them needs a database.
- **No real Telegram in tests.** Nothing in the test suite connects to Telegram. The fakes in
  `tests/userbot/fakes.py` implement the Protocols; their write methods record the attempt and
  raise, so a test can prove that none was made.
- **Test database**: real PostgreSQL, never SQLite. The fixture in `tests/db/conftest.py` reads
  `TEST_DATABASE_URL` from the environment or `.env`, creates a database with a random name through
  an admin connection, applies the Alembic migrations to it, so every database test also runs the
  migration, yields a session factory built by the production code, and drops the database with
  `WITH (FORCE)`, also after a failure. One database per test keeps tests isolated without
  truncation. Unreachable Postgres or a missing variable fails the test with a message naming
  `make db-up`; it never skips. CI runs a Postgres service container on the same tag as compose.
- Planned: scripted fakes for aiogram and the LLM (CYR-12, CYR-13); `respx` for the HTTP client
  (CYR-13). Failure cases to cover for new behaviour:
  empty input, malformed Telegram event, invalid LLM JSON, status changed between scheduling and
  sending, FloodWait.

## Open design questions

Listed in [decisions.md](decisions.md) under "Open questions": how the worker sends through the
userbot session, worker reconnect to Redis, where the layered prompt builder lives, whether `bot`,
`userbot` and `worker` may import each other, and the first real Leomatch match (card format, chat order, search).
