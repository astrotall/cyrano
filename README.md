# Cyrano

![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Pydantic v2](https://img.shields.io/badge/Pydantic-v2-E92063?style=for-the-badge&logo=pydantic&logoColor=white)
![uv](https://img.shields.io/badge/uv-package%20manager-DE5FE9?style=for-the-badge&logo=uv&logoColor=white)
![Ruff](https://img.shields.io/badge/Ruff-lint%20%2B%20format-D7FF64?style=for-the-badge&logo=ruff&logoColor=black)
![mypy strict](https://img.shields.io/badge/mypy-strict-2A6DB2?style=for-the-badge)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)
[![CI](https://github.com/astrotall/cyrano/actions/workflows/ci.yml/badge.svg)](https://github.com/astrotall/cyrano/actions/workflows/ci.yml)

**A Telegram assistant that chats with contacts its owner has approved, in the owner's style, through an LLM, and hands the conversation back to the owner whenever a human has to decide.**

## About the project

A Telethon userbot on a second Telegram account is what contacts talk to. An aiogram control bot
on the owner's main account is the control panel. An Arq worker handles delayed jobs. People come
from a matching bot: the owner likes profiles there by hand, and the system never touches it.

The main risks are replying to the wrong person, replying where a human must decide, account
limits and bans, and leaking personal data. The design follows from that.

Key principles:

- **The owner approves every contact.** An unknown sender is registered and shown to the owner as
  a card. Nothing is sent until the owner presses Reply and chooses a mode.
- **Only active contacts get automatic replies, checked at the sender.** The status is checked
  when a reply is scheduled and again right before it is sent. Every outgoing message goes
  through one guarded sender; no other code path sends.
- **Pacing and limits are central.** Humanized delay, typing action, message splitting, quiet
  hours, rate limits and FloodWait handling live in the sender, not in callers.
- **Stop-triggers hand over.** Meeting or date proposals, phone and contact exchange, money,
  photo requests, a direct question whether it is a bot or AI, and low model confidence stop the
  conversation and notify the owner. The assistant does not answer those itself.
- **Style comes from a persona and examples, not training.** A common persona text, a prompt
  fragment per mode and few-shot examples from the owner's real chats. No model is trained.
- **Personal data stays local.** Persona, examples, sessions, media and messages never go to git.
  Message text, names and other personal data are never logged at INFO or above.

> The project skeleton (CYR-8) and the storage layer (CYR-9) exist. The flow below is the plan.

## Architecture diagram

Planned flow. The boxes marked (planned) do not exist yet.

```
 unknown sender
      |
      v
 +-----------------------+   registers contact (new),     +------------------+
 | userbot (Telethon)    |   collects profile             | PostgreSQL       |
 | second account        |------------------------------->| contacts,        |
 | (planned)             |                                | messages,        |
 +-----------------------+                                | modes            |
      |  card                                              +------------------+
      v                                                           ^
 +-----------------------+  Reply (mode) / Ignore                 |
 | control bot (aiogram) |<--- owner, main account               |
 | owner only (planned)  |                                        |
 +-----------------------+                                        |
      | contact -> active                                         |
      v                                                           |
 incoming message --> Arq job, delayed, debounced  --> Redis      |
                      (planned)                        (exists)   |
      |                                                           |
      v                                                           |
 +-----------------------+   re-check status, build layered       |
 | worker (Arq)          |   prompt, call LLM (DeepSeek)  --------+
 | (skeleton exists)     |
 +-----------------------+
      |  reply text
      v
 +-----------------------------------------------+
 | sender: status guard, rate limits, humanized  |
 | delay, typing, splitting, quiet hours         |  --> Telegram
 | (planned)                                     |
 +-----------------------------------------------+
```

What exists: configuration, JSON logging, the app process with a heartbeat, the Arq worker with a
placeholder job, the contact status rules, the database schema with its migrations and
repositories, Docker and CI. Nothing calls the repositories yet. The userbot, bot and LLM packages
are empty; `services` holds only the repository interfaces. Details:
[.claude/docs/contact-lifecycle.md](.claude/docs/contact-lifecycle.md).

### Code layers

Import direction (enforced by review, and by a test for `domain`, `db` and `services`; full rules in
[.claude/docs/architecture.md](.claude/docs/architecture.md)):

```
 main (composition root: builds the concrete objects and passes them in)
   |
   v
 bot, userbot, worker  -->  services  -->  interfaces defined in services
                                              ^                ^
                                              |                |
                                     llm fits them     db fits them
                                     (no import of services)

 config, domain, prompts: leaves, imported by anyone, import no layer
 llm and db never import services or each other
```

## Stack

| Layer | Technology | In the project |
| --- | --- | --- |
| Language | Python 3.14, uv | now |
| Configuration | pydantic-settings, Pydantic v2 | now |
| Time zones | tzdata (IANA database in the slim image) | now |
| Delayed jobs | Arq on Redis | now (skeleton worker, placeholder job) |
| Cache and queue | Redis 7.4 (AOF) | now (compose service) |
| Database server | PostgreSQL 16.15 | now (compose service, migrations, tests) |
| Userbot | Telethon | planned, CYR-10 |
| Control bot | aiogram | planned, CYR-12 |
| ORM and migrations | SQLAlchemy 2.x (async), asyncpg, Alembic | now (schema and repositories; nothing calls them yet) |
| LLM client | DeepSeek over plain httpx, no SDK | planned, CYR-13 |
| Lint, format | Ruff | now |
| Types | mypy strict with the Pydantic plugin | now |
| Tests | pytest, pytest-asyncio | now |
| HTTP mocks in tests | respx | planned, with CYR-13 |
| Containers | Docker, Docker Compose | now |
| CI | GitHub Actions, Dependabot | now |

## Repository layout

What exists now:

```
.
├── CLAUDE.md                     # read first: decisions and hard rules
├── CONTRIBUTING.md               # branches, commits, Jira, pull requests
├── README.md
├── pyproject.toml                # dependencies, Ruff, mypy, pytest config
├── uv.lock
├── .python-version               # 3.14
├── Makefile                      # install, check, test, fmt, up, down, logs, migrate, revision, db-up
├── Dockerfile                    # multi-stage, non-root runtime
├── docker-compose.yml            # app, worker, one-shot migrate, postgres, redis
├── docker-compose.override.yml   # dev: Postgres and Redis on 127.0.0.1
├── .env.example                  # bare template, no comments
├── alembic.ini                   # points at migrations/; no URL, it comes from Settings
├── migrations/
│   ├── env.py                    # async environment, URL from Settings
│   └── versions/                 # 0001 creates the tables and seeds the default mode
├── .dockerignore
├── .gitignore
├── .claude/docs/
│   ├── architecture.md           # layers, import rules, processes, config, logging
│   ├── contact-lifecycle.md      # statuses, transitions, message flow
│   ├── safety-rules.md           # S1..S10
│   └── decisions.md              # decided and open
├── .github/
│   ├── dependabot.yml            # weekly: Actions, Docker, Compose, uv
│   └── workflows/ci.yml
├── src/cyrano/
│   ├── __main__.py               # python -m cyrano
│   ├── main.py                   # app entrypoint, signals, heartbeat
│   ├── config.py                 # Settings, load_settings, ConfigError
│   ├── logging.py                # JSON-lines logging
│   ├── health.py                 # heartbeat file and the app healthcheck
│   ├── userbot/                  # empty skeleton
│   ├── bot/                      # empty skeleton
│   ├── llm/                      # empty skeleton
│   ├── domain/                   # enums, status transition rules, errors, Pydantic models
│   ├── db/                       # SQLAlchemy models, engine and sessions, repositories
│   ├── services/
│   │   └── repositories.py       # repository Protocols, the rest of the package is planned
│   └── worker/
│       └── settings.py           # Arq WorkerSettings, placeholder job
└── tests/                        # mirrors src/cyrano; database tests need a real PostgreSQL
```

## Running

Requirements: Python 3.14 and [uv](https://docs.astral.sh/uv/). Docker with Compose v2 for the
full stack.

### Variables

```sh
cp .env.example .env   # REQUIRED before `docker compose up` / `make up`; then fill in the values
```

`.env` is git-ignored; never commit it. `.env.example` is a bare template without comments: the
required variables plus `MEDIA_DIR`, `POSTGRES_*` and `TEST_DATABASE_URL`. Every variable, required
and optional, is described in this README ([Settings](#settings) and the table below). Optional
variables are overrides: add them to `.env` yourself when you need a value other than the default.
Empty values count as unset. If a required variable is missing or invalid, the app exits with code
2 and lists every problem at once; the worker exits with the same message.

| Variable | Required | Without it |
| --- | --- | --- |
| `TELEGRAM_API_ID` | yes | app and worker refuse to start |
| `TELEGRAM_API_HASH` | yes | same |
| `USERBOT_SESSION_PATH` | yes | same |
| `BOT_TOKEN` | yes | same |
| `OWNER_TELEGRAM_ID` | yes | same |
| `DEEPSEEK_API_KEY` | yes | same |
| `DATABASE_URL` | yes, must start with `postgresql+asyncpg://` | same |
| `REDIS_URL` | yes, must start with `redis://` or `rediss://` | same |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | for Docker only | compose refuses to start |
| `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT` | optional override, Docker only; not in `.env.example` | ports 5432 and 6379 |
| `TEST_DATABASE_URL` | for `make test` only, not an application setting | database tests fail with a message naming `make db-up` |

The skeleton does not call Telegram or DeepSeek, so validation only checks types and formats.

The template holds the values for running on the host with Postgres and Redis from compose:
`DATABASE_URL` and `REDIS_URL` point at `localhost`, `USERBOT_SESSION_PATH` and `MEDIA_DIR` are
relative paths, and `POSTGRES_*` feed the compose Postgres. The two ways of running are described
in [Run with Docker](#run-with-docker) and [Run locally](#run-locally); one `.env` serves both.

In Docker, `POSTGRES_*` is the single source of truth for credentials. Compose builds
`DATABASE_URL` (host `postgres`) and `REDIS_URL` (host `redis`) for `app` and `worker` from them
and sets `USERBOT_SESSION_PATH` and `MEDIA_DIR` to the container paths. The localhost values in
`.env` are ignored inside the containers. When Postgres comes from compose, the user, password and
database in the local `DATABASE_URL` must match `POSTGRES_USER`, `POSTGRES_PASSWORD` and
`POSTGRES_DB`. `POSTGRES_PASSWORD` in the template is a placeholder: change it before any real
deployment. The password goes into a URL unescaped, so use only the URL-safe characters letters,
digits and `-` `.` `_` `~` (for example `openssl rand -hex 24`).

### Run with Docker

```sh
cp .env.example .env   # once
make up      # docker compose up -d --build: migrate, app, worker, postgres, redis
make logs    # follow logs; app and worker each print a "started" line
make down    # stop everything (named volumes are kept)
```

- **Migrations.** The one-shot `migrate` service (same image, `alembic upgrade head`) runs after
  Postgres is healthy and exits. `app` and `worker` start only if it exits with 0. It reads the same
  `Settings`, so it refuses to start with the same message when a required variable is missing; if
  it fails, `docker compose logs migrate` shows why.
- **Healthchecks.** All four long-running services report health (`docker compose ps`). `worker` uses Arq's
  check (`arq --check`). `app` has no HTTP server: its event loop touches a heartbeat file every
  10 s and the check (`python -m cyrano.health`) fails if the file is older than 60 s.
- **Redis persistence.** Redis runs with AOF (`--appendonly yes`) on the `redis_data` volume, so
  Arq's delayed jobs survive restarts.
- **Pinned images.** `postgres:16.15-alpine` and `redis:7.4-alpine`. Dependabot proposes bumps
  weekly (`.github/dependabot.yml`).
- **Volumes.** `postgres_data`, `redis_data`, `userbot_session` (app only), `media` (app and
  worker). `down` keeps them.

### Postgres and Redis from the host

`docker-compose.override.yml` is picked up automatically by `docker compose up` and publishes
Postgres and Redis **on `127.0.0.1` only**, so `psql` and a locally run app can reach the compose
services:

```sh
make db-up   # starts only Postgres, with the override, and waits until it is healthy
psql "postgresql://cyrano:change-me@127.0.0.1:5432/cyrano"   # values from POSTGRES_* in .env
redis-cli -h 127.0.0.1 ping
```

If ports 5432 or 6379 are taken, set `POSTGRES_HOST_PORT` or `REDIS_HOST_PORT` in `.env` and use
the same ports in the local `DATABASE_URL`, `TEST_DATABASE_URL` and `REDIS_URL`. To run without the
override, for example on a server: `docker compose -f docker-compose.yml up -d`.

### Run locally

```sh
make install   # uv sync --locked
make db-up     # Postgres from compose; needed by the next line
make migrate   # alembic upgrade head against DATABASE_URL
uv run python -m cyrano                                                   # app
uv run arq --custom-log-dict cyrano.worker.settings.LOG_CONFIG \
    cyrano.worker.settings.WorkerSettings                                 # worker
```

`.env` in the current directory is loaded; real environment variables take precedence over it.
The app builds a database engine but opens no connection until a later ticket uses it, and it does
not connect to Redis. The worker connects to Redis; the compose one works via
`docker compose up -d redis`.

### Migrations

Alembic lives in `alembic.ini` and `migrations/` at the repository root. The URL comes from
`Settings.database_url`, so `make migrate` and `make revision` need a complete `.env`.

```sh
make migrate                       # alembic upgrade head
make revision m="add some column"  # autogenerate a revision, then ruff fix and format it
uv run alembic downgrade -1        # step back one revision
```

A generated revision is a draft: review it and remove the marker comments Alembic writes into it
(no comments in source, hard rule 1). Autogenerate does not see changes to enum values, and it does
not drop enum types on downgrade; write those by hand.

## Settings

Defined in `src/cyrano/config.py`. Secrets are `SecretStr` and are masked in logs. This section
describes every application variable. `.env.example` holds the required ones and a few local
values; the other optional variables are overrides you add to `.env` yourself, and without them
the default applies.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` | yes | | Telegram API credentials of the second account (the userbot). Create them on my.telegram.org, menu "API development tools" |
| `USERBOT_SESSION_PATH` | yes | | Where the Telethon session file is stored |
| `BOT_TOKEN` | yes | | Control bot token from @BotFather |
| `OWNER_TELEGRAM_ID` | yes | | Telegram id of the main account; the only user the control bot will answer (planned, CYR-12) |
| `DEEPSEEK_API_KEY` | yes | | DeepSeek API key |
| `DEEPSEEK_MODEL` | no | `deepseek-chat` | DeepSeek model name |
| `DATABASE_URL` | yes | | PostgreSQL URL, must start with `postgresql+asyncpg://` |
| `REDIS_URL` | yes | | Redis URL, must start with `redis://` or `rediss://` |
| `MEDIA_DIR` | no | `media` | Where downloaded media goes |
| `LOG_LEVEL` | no | `INFO` | Level of the application log. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, case-insensitive |
| `TELETHON_LOG_LEVEL`, `AIOGRAM_LOG_LEVEL` | no | `WARNING` | Level of the `telethon` and `aiogram` loggers; same allowed values as `LOG_LEVEL` |
| `HTTPX_LOG_LEVEL` | no | `WARNING` | Level of the `httpx` logger, also applied to `httpcore`; same allowed values as `LOG_LEVEL` |
| `TIMEZONE` | no | `Europe/Moscow` | IANA zone name, validated at startup |
| `HEARTBEAT_PATH` | no | `<tmpdir>/cyrano-heartbeat` | Liveness file touched by the app and read by the app healthcheck |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | for Docker | | Credentials and database name of the compose Postgres; see [Variables](#variables) |
| `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT` | no, Docker only | `5432`, `6379` | Host ports that the override file publishes on `127.0.0.1` |
| `TEST_DATABASE_URL` | for `make test` | | Not an application setting; see [Testing](#testing) |

A relative `USERBOT_SESSION_PATH` or `MEDIA_DIR` is kept as written; when something opens it
(nothing does yet), it is relative to the current working directory of the process. Inside the
containers compose sets both to absolute paths.

Logs are JSON lines on stdout for the app, the worker and Arq's own lines. Timestamps in log lines
are UTC.

## Testing

```sh
make db-up     # once: start Postgres from compose
make test      # uv run pytest
```

pytest with `asyncio_mode = auto`; tests live in `tests/` and mirror `src/cyrano`. Current tests
cover configuration, logging (including secret masking), the heartbeat and healthcheck, the app
loop, a real-process SIGTERM check, the Arq worker settings, the status transition rules, the
migration and the repositories. All tests use fake values only.

Database tests run against real PostgreSQL, never SQLite, because JSONB and native enums must be
tested for real. `TEST_DATABASE_URL` (from the environment or `.env`; the template value targets
the compose Postgres on `127.0.0.1:5432`) points at a server and a database to connect to for
admin statements. It is not an application setting. Each
test creates a database with a random name there, applies the Alembic migrations to it, and drops
it afterwards, so the role needs `CREATEDB`; the compose `POSTGRES_USER` has it. If the host port is
not 5432, use the same port as `POSTGRES_HOST_PORT`. If Postgres is unreachable or the variable is
missing, the tests fail with a message that names `make db-up`; they never skip.

## Linters, types and git hooks

```sh
make check     # ruff check, ruff format --check, mypy (strict)
make fmt       # ruff check --fix, ruff format
```

Ruff: line length 100, target `py314`, rule sets E, W, F, I, B, UP, SIM, ASYNC, RUF, S. mypy runs
in strict mode with the Pydantic plugin over `src` and `tests`.

Local git hooks are planned (CYR-29). Until they land, run `make check` and `make test` before
every commit.

## What CI runs

`.github/workflows/ci.yml` runs on every push and pull request; a newer run on the same ref
cancels the older one.

| Job | What it does |
| --- | --- |
| `check` | a `postgres:16.15-alpine` service container, installs uv and the Python from `.python-version`, `make install`, `make check`, `make test` with `TEST_DATABASE_URL` set |
| `docker` | `cp .env.example .env`, `docker compose config --quiet` with and without the override file and for the `migrate` service, `docker compose build`, `alembic heads` in a throwaway `migrate` container to prove the image holds the migrations; nothing is pushed and no long-running service is started |

## More

- [CLAUDE.md](CLAUDE.md): locked decisions and hard rules.
- [CONTRIBUTING.md](CONTRIBUTING.md): branches, commits, Jira, pull requests.
- [.claude/docs/architecture.md](.claude/docs/architecture.md)
- [.claude/docs/contact-lifecycle.md](.claude/docs/contact-lifecycle.md)
- [.claude/docs/safety-rules.md](.claude/docs/safety-rules.md)
- [.claude/docs/decisions.md](.claude/docs/decisions.md)
