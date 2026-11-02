# Architecture

State as of CYR-8: only the skeleton exists. Anything else is marked **planned** with its ticket.
Product and decisions: [CLAUDE.md](../../CLAUDE.md). Reasons: [decisions.md](decisions.md).

## Layers

Source lives in `src/cyrano/`.

| Package or module | Role | State |
| --- | --- | --- |
| `userbot/` | Telethon userbot: receives messages on the second account, converts events into plain objects, performs the actual Telegram send | empty skeleton; planned CYR-10, CYR-11 |
| `bot/` | aiogram control bot: owner-only, contact card, Reply/Ignore buttons, notifications | empty skeleton; planned CYR-12 |
| `llm/` | LLM client: DeepSeek over plain httpx, response parsing | empty skeleton; planned CYR-13 |
| `db/` | Engine, sessions, models, repositories | empty skeleton; planned CYR-9 |
| `services/` | Business logic shared by userbot, bot and worker: contact lifecycle, sender, reply pipeline | empty skeleton; planned from CYR-12 on |
| `worker/` | Arq worker: `settings.py` holds `WorkerSettings`, `startup`, `shutdown`, a placeholder `noop` job (Arq refuses to start a worker with no registered functions) | skeleton works; real jobs planned CYR-17 |
| `config.py` | `Settings` (pydantic-settings), `load_settings`, `ConfigError` | exists |
| `logging.py` | JSON-lines formatter, `build_log_config`, `configure_logging` | exists |
| `health.py` | Heartbeat file, `is_alive`, `python -m cyrano.health` | exists |
| `main.py`, `__main__.py` | App entrypoint, signal handling, heartbeat task | exists; userbot and bot are plugged in by later tickets |
| `domain` | Plain data types shared by layers (statuses, DTOs) | planned, not created |
| `prompts` | Prompt texts, few-shot data loading, trigger lists | planned, not created; the layered builder's location is decided in CYR-16 |

`tests/` mirrors the packages (`tests/userbot`, `tests/bot`, `tests/llm`, `tests/db`,
`tests/services`, `tests/worker`); the skeleton packages have empty test packages.

## Import direction

Planned; enforced by review (and by a test if one is added later).

1. `bot`, `userbot` and `worker` may import `services`.
2. `services` reach `llm` and `db` only through interfaces defined in `services`. The concrete
   `llm` and `db` code satisfies those interfaces without importing `services`.
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
| SQLAlchemy models, repositories, session handling | `db` |
| Arq job functions and worker settings | `worker` |
| A data type used by several layers | `domain` (planned) |
| Prompt text, few-shot loading, trigger lists | `prompts` (planned) or config |
| An environment-driven setting | `config.py` and `.env.example`, together |
| Wiring concrete objects together | `main.py` |
| Alembic migrations | decided in CYR-9 |

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

- **app**: `python -m cyrano`. Today it loads config, starts JSON logging, touches the heartbeat
  file and waits for SIGTERM or SIGINT. Planned: the userbot and the control bot run on its event
  loop (CYR-10, CYR-12). On shutdown it stops the heartbeat and removes the file; planned: stop the
  userbot and the control bot and close the database and Redis connections there. It has the
  `userbot_session` volume.
- **worker**: `arq cyrano.worker.settings.WorkerSettings`. Arq imports `settings.py` itself, so a
  missing or invalid configuration ends the import with `SystemExit` and the readable `ConfigError`
  text instead of a traceback chain. Planned: runs the delayed reply job
  (CYR-17) and background jobs (CYR-23, CYR-24, CYR-25). It has the `media` volume but not the
  session volume; how it sends through the userbot session is an open question (CYR-17).
- **postgres**: `postgres:16.15-alpine`, volume `postgres_data`. Nothing uses it yet.
- **redis**: `redis:7.4-alpine` with AOF on `redis_data`. Arq stores delayed jobs here.
- The Dockerfile builds one image for app and worker (Python 3.14 slim, uv only in the builder
  stage, non-root user `cyrano`, uid 10001). Compose overrides the command per service.
- `depends_on` waits for healthy postgres and redis before app and worker start.

### Profile parsing (planned, CYR-11)

People arrive from a matching bot. The first messages from a contact may carry the text of their
profile in that bot (for Leomatch); the userbot collects it together with nick, name, bio and
photo for the card. The format and how it is parsed are specified in CYR-11. The system never
sends anything to the matching bot.

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

- **Secrets** are `SecretStr`: `repr`, `str` and dumps mask them; `.get_secret_value()` is called
  only where the value is used. Settings that carry URLs with credentials are secrets too.
- **Local vs Docker** (CYR-8): `.env.example` has a local section (`DATABASE_URL` and `REDIS_URL`
  on `localhost`, relative `USERBOT_SESSION_PATH` and `MEDIA_DIR`) and a Docker section
  (`POSTGRES_*`). Compose builds `DATABASE_URL` and `REDIS_URL` for app and worker from
  `POSTGRES_*` and the service names and sets `USERBOT_SESSION_PATH=/data/session/userbot` and
  `MEDIA_DIR=/data/media`; `environment:` beats `env_file:`, so the localhost values are ignored
  in containers.
- **Time**: timestamps are stored in UTC; `TIMEZONE` is applied only at the edges (planned
  users: quiet hours in CYR-18 and times shown to the owner). `Settings.tzinfo` returns the `ZoneInfo`. `tzdata` is a
  dependency because the slim image has no system zone database.
- **New setting**: add the field to `Settings` and the variable to `.env.example` in the same
  commit.

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
  healthcheck, the app loop, a real subprocess that must exit cleanly on SIGTERM, worker settings.
- Planned: scripted fakes for Telethon, aiogram and the LLM (CYR-10, CYR-12, CYR-13); `respx` for
  the HTTP client (CYR-13); a database fixture (CYR-9). Failure cases to cover for new behaviour:
  empty input, malformed Telegram event, invalid LLM JSON, status changed between scheduling and
  sending, FloodWait.

## Open design questions

Listed in [decisions.md](decisions.md) under "Open questions": how the worker sends through the
userbot session, how migrations run, worker reconnect to Redis, where the layered prompt builder
lives, whether `bot`, `userbot` and `worker` may import each other.
