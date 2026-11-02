# Architecture

State as of CYR-9: the skeleton and the storage layer exist. Anything else is marked **planned**
with its ticket.
Product and decisions: [CLAUDE.md](../../CLAUDE.md). Reasons: [decisions.md](decisions.md).

## Layers

Source lives in `src/cyrano/`.

| Package or module | Role | State |
| --- | --- | --- |
| `userbot/` | Telethon userbot: receives messages on the second account, converts events into plain objects, performs the actual Telegram send | empty skeleton; planned CYR-10, CYR-11 |
| `bot/` | aiogram control bot: owner-only, contact card, Reply/Ignore buttons, notifications | empty skeleton; planned CYR-12 |
| `llm/` | LLM client: DeepSeek over plain httpx, response parsing | empty skeleton; planned CYR-13 |
| `db/` | SQLAlchemy models (`models.py`), engine and session factory (`session.py`), repositories (`repositories.py`) | exists; nothing calls it yet |
| `services/` | Business logic shared by userbot, bot and worker: contact lifecycle, sender, reply pipeline. `repositories.py` holds the repository Protocols | Protocols exist; the logic is planned from CYR-12 on |
| `worker/` | Arq worker: `settings.py` holds `WorkerSettings`, `startup`, `shutdown`, a placeholder `noop` job (Arq refuses to start a worker with no registered functions) | skeleton works; real jobs planned CYR-17 |
| `config.py` | `Settings` (pydantic-settings), `load_settings`, `ConfigError` | exists |
| `logging.py` | JSON-lines formatter, `build_log_config`, `configure_logging` | exists |
| `health.py` | Heartbeat file, `is_alive`, `python -m cyrano.health` | exists |
| `main.py`, `__main__.py` | App entrypoint, signal handling, heartbeat task | exists; userbot and bot are plugged in by later tickets |
| `domain/` | Leaf package, imports only pydantic and the standard library: enums, status transition rules, domain errors, Pydantic models the layers exchange | exists |
| `prompts` | Prompt texts, few-shot data loading, trigger lists | planned, not created; the layered builder's location is decided in CYR-16 |

`tests/` mirrors the packages (`tests/userbot`, `tests/bot`, `tests/llm`, `tests/domain`,
`tests/db`, `tests/services`, `tests/worker`); the skeleton packages have empty test packages.

## Import direction

Enforced by review. `tests/test_import_direction.py` also checks the part that exists: `domain`
imports only pydantic, the standard library and itself; `db` imports neither `services` nor `llm`;
`services` does not import `db`.

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
               userbot + control bot (planned)       delayed jobs (planned)
               heartbeat task (exists)               placeholder job (exists)
                  |        |                            |        |
                  v        v                            v        v
              PostgreSQL  Redis  <---- Arq jobs ----->  Redis  PostgreSQL
                                                          |
                                                          v
                                                     LLM (DeepSeek)
```

- **app**: `python -m cyrano`. Today it loads config, starts JSON logging, builds the database
  engine (which opens no connection until something uses it), touches the heartbeat file and waits
  for SIGTERM or SIGINT. Planned: the userbot and the control bot run on its event loop (CYR-10,
  CYR-12). On shutdown it stops the heartbeat, removes the file and disposes the engine; planned:
  stop the userbot and the control bot and close the Redis connections there. It has the
  `userbot_session` volume.
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

### Profile parsing (planned, CYR-11)

People arrive from a matching bot. The first messages from a contact may carry the text of their
profile in that bot (for Leomatch); the userbot collects it together with nick, name, bio and
photo for the card. The format and how it is parsed are specified in CYR-11. The system never
sends anything to the matching bot.

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
  healthcheck, the app loop, a real subprocess that must exit cleanly on SIGTERM, worker settings,
  status transition rules (pure, no database), domain models, migrations and repositories.
- **Test database**: real PostgreSQL, never SQLite. The fixture in `tests/db/conftest.py` reads
  `TEST_DATABASE_URL` from the environment or `.env`, creates a database with a random name through
  an admin connection, applies the Alembic migrations to it, so every database test also runs the
  migration, yields a session factory built by the production code, and drops the database with
  `WITH (FORCE)`, also after a failure. One database per test keeps tests isolated without
  truncation. Unreachable Postgres or a missing variable fails the test with a message naming
  `make db-up`; it never skips. CI runs a Postgres service container on the same tag as compose.
- Planned: scripted fakes for Telethon, aiogram and the LLM (CYR-10, CYR-12, CYR-13); `respx` for
  the HTTP client (CYR-13). Failure cases to cover for new behaviour:
  empty input, malformed Telegram event, invalid LLM JSON, status changed between scheduling and
  sending, FloodWait.

## Open design questions

Listed in [decisions.md](decisions.md) under "Open questions": how the worker sends through the
userbot session, worker reconnect to Redis, where the layered prompt builder lives, whether `bot`,
`userbot` and `worker` may import each other.
