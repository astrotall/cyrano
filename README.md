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

> Only the project skeleton exists (CYR-8). The flow below is the plan; see [Status](#status).

## Architecture diagram

Planned flow. The boxes marked (planned) do not exist yet.

```
 unknown sender
      |
      v
 +-----------------------+   registers contact (new),     +------------------+
 | userbot (Telethon)    |   collects profile             | PostgreSQL       |
 | second account        |------------------------------->| contacts,        |
 | (planned)             |                                | messages         |
 +-----------------------+                                | (planned)        |
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

Only the skeleton exists: configuration, JSON logging, the app process with a heartbeat, the Arq
worker with a placeholder job, Docker and CI. The userbot, bot, LLM, database and services
packages are empty. Details: [.claude/docs/contact-lifecycle.md](.claude/docs/contact-lifecycle.md).

### Code layers

Import direction (planned, enforced by review; full rules in
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
| Database server | PostgreSQL 16.15 | now (compose service, nothing uses it yet) |
| Userbot | Telethon | planned, CYR-10 |
| Control bot | aiogram | planned, CYR-12 |
| ORM and migrations | SQLAlchemy 2.0 (async), asyncpg, Alembic | planned, CYR-9 |
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
├── Makefile                      # install, check, test, fmt, up, down, logs
├── Dockerfile                    # multi-stage, non-root runtime
├── docker-compose.yml            # app, worker, postgres, redis
├── docker-compose.override.yml   # dev: Postgres and Redis on 127.0.0.1
├── .env.example                  # every variable, two run modes
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
│   ├── db/                       # empty skeleton
│   ├── services/                 # empty skeleton
│   └── worker/
│       └── settings.py           # Arq WorkerSettings, placeholder job
└── tests/                        # mirrors src/cyrano; empty packages for the skeletons
```

## Running

Requirements: Python 3.14 and [uv](https://docs.astral.sh/uv/). Docker with Compose v2 for the
full stack.

### Variables

```sh
cp .env.example .env   # REQUIRED before `docker compose up` / `make up`; then fill in the values
```

`.env` is git-ignored; never commit it. `.env.example` documents every variable. Empty values
count as unset. If a required variable is missing or invalid, the app exits with code 2 and lists
every problem at once; the worker exits with the same message.

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
| `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT` | optional, Docker only | ports 5432 and 6379 |

The skeleton does not call Telegram or DeepSeek, so validation only checks types and formats.

`.env.example` has two sections plus a shared part:

| Section | Used when | What it holds |
| --- | --- | --- |
| Shared | always | Telegram, LLM and optional app settings |
| 1. Local run | app and worker run on the host (`uv run ...`) | `DATABASE_URL` and `REDIS_URL` pointing at `localhost`, relative `USERBOT_SESSION_PATH` and `MEDIA_DIR` |
| 2. Docker | `docker compose up` | `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, host ports |

In Docker, `POSTGRES_*` is the single source of truth for credentials. Compose builds
`DATABASE_URL` (host `postgres`) and `REDIS_URL` (host `redis`) for `app` and `worker` from them
and sets `USERBOT_SESSION_PATH` and `MEDIA_DIR` to the container paths. The localhost values in
`.env` are ignored inside the containers, so one `.env` serves both ways of running. The password
goes into a URL unescaped, so use URL-safe characters only (for example `openssl rand -hex 24`).

### Run with Docker

```sh
cp .env.example .env   # once
make up      # docker compose up -d --build: app, worker, postgres, redis
make logs    # follow logs; app and worker each print a "started" line
make down    # stop everything (named volumes are kept)
```

- **Healthchecks.** All four services report health (`docker compose ps`). `worker` uses Arq's
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
psql "postgresql://cyrano:change-me@127.0.0.1:5432/cyrano"   # values from POSTGRES_* in .env
redis-cli -h 127.0.0.1 ping
```

If ports 5432 or 6379 are taken, set `POSTGRES_HOST_PORT` or `REDIS_HOST_PORT` in `.env` and use
the same ports in the local `DATABASE_URL` and `REDIS_URL`. To run without the override, for
example on a server: `docker compose -f docker-compose.yml up -d`.

### Run locally

```sh
make install   # uv sync --locked
uv run python -m cyrano                                                   # app
uv run arq --custom-log-dict cyrano.worker.settings.LOG_CONFIG \
    cyrano.worker.settings.WorkerSettings                                 # worker
```

`.env` in the current directory is loaded; real environment variables take precedence over it.
The skeleton app does not connect to Postgres or Redis. The worker connects to Redis; the compose
one works via `docker compose up -d redis`.

## Settings

Defined in `src/cyrano/config.py`. Secrets are `SecretStr` and are masked in logs.

| Group | Variables (default for optional ones) |
| --- | --- |
| Telegram | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` (second account, from my.telegram.org), `USERBOT_SESSION_PATH`, `BOT_TOKEN` (from @BotFather), `OWNER_TELEGRAM_ID` (main account; the only user the control bot will answer, planned CYR-12) |
| LLM | `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL` (`deepseek-chat`) |
| Storage | `DATABASE_URL`, `REDIS_URL`, `MEDIA_DIR` (`media`) |
| Logging | `LOG_LEVEL` (`INFO`), `TELETHON_LOG_LEVEL`, `AIOGRAM_LOG_LEVEL`, `HTTPX_LOG_LEVEL` (each `WARNING`; the last also covers `httpcore`) |
| Runtime | `TIMEZONE` (`Europe/Moscow`, validated as an IANA name), `HEARTBEAT_PATH` (`<tmpdir>/cyrano-heartbeat`) |

Logs are JSON lines on stdout for the app, the worker and Arq's own lines. Timestamps in log lines
are UTC.

## Testing

```sh
make test      # uv run pytest
```

pytest with `asyncio_mode = auto`; tests live in `tests/` and mirror `src/cyrano`. Current tests
cover configuration, logging (including secret masking), the heartbeat and healthcheck, the app
loop, a real-process SIGTERM check and the Arq worker settings. All tests use fake values only.

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
| `check` | installs uv and the Python from `.python-version`, `make install`, `make check`, `make test` |
| `docker` | `cp .env.example .env`, `docker compose config --quiet` with and without the override file, `docker compose build`; nothing is pushed or started |

## Status

Done:

- ✅ CYR-8 project skeleton: config, JSON logging, app process with heartbeat, Arq worker
  skeleton, Docker Compose, CI, Dependabot.
- ✅ CYR-7 documentation (this set).

MVP, planned:

- ⏳ CYR-9 DB models and Alembic
- ⏳ CYR-10 Telethon userbot
- ⏳ CYR-11 contact profile collection
- ⏳ CYR-12 control bot and contact card
- ⏳ CYR-13 LLM client (DeepSeek)
- ⏳ CYR-14 persona and style examples import
- ⏳ CYR-15 conversation modes
- ⏳ CYR-16 layered prompt builder
- ⏳ CYR-17 Arq worker, delayed reply with debounce
- ⏳ CYR-18 humanization
- ⏳ CYR-19 reply only to active contacts
- ⏳ CYR-20 stop-triggers
- ⏳ CYR-21 auto-pause on manual messages
- ⏳ CYR-22 rate limits and FloodWait
- ⏳ CYR-29 local git hooks

Post-MVP, planned:

- ⏳ CYR-23 proactive messages
- ⏳ CYR-24 reminders for undecided cards
- ⏳ CYR-25 contact summaries
- ⏳ CYR-26 catalog command
- ⏳ CYR-27 modes management
- ⏳ CYR-28 observability

Risks:

- ⚠️ Account limits and bans. A userbot account can be limited or banned by Telegram; pacing and
  rate limits reduce the risk and cannot remove it.
- ⚠️ Reply quality depends on the model and on the quality of the persona and examples.

## More

- [CLAUDE.md](CLAUDE.md): locked decisions and hard rules.
- [CONTRIBUTING.md](CONTRIBUTING.md): branches, commits, Jira, pull requests.
- [.claude/docs/architecture.md](.claude/docs/architecture.md)
- [.claude/docs/contact-lifecycle.md](.claude/docs/contact-lifecycle.md)
- [.claude/docs/safety-rules.md](.claude/docs/safety-rules.md)
- [.claude/docs/decisions.md](.claude/docs/decisions.md)
