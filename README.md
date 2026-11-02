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

> The project skeleton (CYR-8), the storage layer (CYR-9) and the userbot intake (CYR-10) exist.
> The flow below is the plan.

## Architecture diagram

Planned flow. The boxes marked (planned) do not exist yet. The userbot box exists for listening and
registering contacts only.

```
 unknown sender
      |
      v
 +-----------------------+   registers contact (new),     +------------------+
 | userbot (Telethon)    |   collects profile             | PostgreSQL       |
 | second account        |------------------------------->| contacts,        |
 | (registers, exists)   |                                | messages,        |
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
repositories, Docker and CI, and the userbot: it logs the second account in, listens for private
messages, registers unknown senders as contacts with status `new`, stores their messages and
publishes an in-memory event for each new contact. It never sends anything. The bot and LLM
packages are empty; `services` holds the repository interfaces, the intake and the event queue.
Details: [.claude/docs/contact-lifecycle.md](.claude/docs/contact-lifecycle.md).

### What the userbot does and does not do

Does:

- listens on the second account and handles only real private messages from real users;
- registers an unknown sender as a contact with status `new` and stores the message (a photo or
  sticker counts too: the text is the caption or empty, the media is not downloaded);
- processes messages that arrived while the app was down (catch-up on start); a redelivered message
  is stored once.

Does not:

- send anything, mark messages as read or answer anyone, unknown senders included. Replies are a
  later ticket and will go only through the guarded sender;
- react to groups, channels, bots, deleted accounts, Telegram's service account or its own
  messages;
- change a status. The step from `new` to `pending` belongs to the control bot (CYR-12).

### Code layers

Import direction (enforced by review, and by a test for `domain`, `db`, `services` and `userbot`,
which is also the only package that may import Telethon; full rules in
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
| Userbot | Telethon | now (listens and registers contacts; sends nothing) |
| Control bot | aiogram | planned, CYR-12 |
| ORM and migrations | SQLAlchemy 2.x (async), asyncpg, Alembic | now (schema and repositories, used by the intake) |
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
├── Makefile                      # install, check, test, fmt, up, down, logs, migrate, revision, db-up, login, login-docker
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
│   ├── __main__.py               # python -m cyrano [login]
│   ├── main.py                   # app entrypoint, login subcommand, signals, heartbeat
│   ├── config.py                 # Settings, load_settings, ConfigError
│   ├── logging.py                # JSON-lines logging
│   ├── health.py                 # heartbeat file and the app healthcheck
│   ├── userbot/                  # Telethon: protocols, filters, handler, client, login, runner
│   ├── bot/                      # empty skeleton
│   ├── llm/                      # empty skeleton
│   ├── domain/                   # enums, status transition rules, errors, Pydantic models
│   ├── db/                       # SQLAlchemy models, engine and sessions, repositories
│   ├── services/
│   │   ├── repositories.py       # repository Protocols
│   │   ├── intake.py             # IntakeService: register contact and message
│   │   ├── sanitize.py           # NUL-byte removal
│   │   └── contact_events.py     # ContactEventPublisher, bounded in-memory queue
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

Validation only checks types and formats; it never calls Telegram or DeepSeek. The app itself
connects to Telegram once a session exists (see [Telegram login](#telegram-login)).

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

### Telegram login

The userbot needs a session for the **second** Telegram account. That account is the one contacts
talk to; it is separate from your main account, which owns the control bot. Use a number you can
receive a login code on.

1. **API credentials.** Log in at <https://my.telegram.org> with the second account's number, open
   "API development tools", create an application and copy `api_id` and `api_hash` into
   `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` in `.env`.
2. **Log in once.** Locally:

   ```sh
   make login          # uv run python -m cyrano login
   ```

   In Docker (same image, same volume as the app, so the app finds the session):

   ```sh
   make login-docker   # docker compose run --rm app python -m cyrano login
   ```

   The command asks for the phone number, then for the code Telegram sends, and for the two-step
   verification password only if the account has one. It prints only that the session was saved.
   Phone, code and password are never stored in a file, in `Settings` or in the logs.
   `make login-docker` also starts Postgres, Redis and the one-shot `migrate` service, because
   `app` depends on them; it needs an interactive terminal.
3. **Start the app.** `python -m cyrano` or `make up`. Without a session file the app exits at once
   with "run `make login`" and does not touch the network; with a revoked session it says so too.
   Run the login before `make up`, otherwise the container keeps restarting with that message.

**The session file is a secret.** It is `USERBOT_SESSION_PATH` plus `.session` (for example
`data/session/userbot.session` locally, `/data/session/userbot.session` in the `userbot_session`
volume). Whoever has it controls the second account without a code. It is created with mode `0600`
and is git-ignored and docker-ignored; never commit, copy or share it. To revoke it, end the session
in Telegram under Settings, Devices.

### Run with Docker

```sh
cp .env.example .env   # once
make login-docker   # once, before the first start (see Telegram login)
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
- **Volumes.** `postgres_data`, `redis_data`, `userbot_session` (app and the login command),
  `media` (app and worker). `down` keeps them.

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
make login     # once: create the Telegram session
uv run python -m cyrano                                                   # app
uv run arq --custom-log-dict cyrano.worker.settings.LOG_CONFIG \
    cyrano.worker.settings.WorkerSettings                                 # worker
```

`.env` in the current directory is loaded; real environment variables take precedence over it.
The app connects to Telegram and to Postgres (when the first message arrives); it does not connect
to Redis. The worker connects to Redis; the compose one works via `docker compose up -d redis`.

### Makefile targets

| Target | What it does |
| --- | --- |
| `make install` | `uv sync --locked` |
| `make check` | Ruff lint, Ruff format check, mypy strict |
| `make test` | pytest; database tests need `make db-up` |
| `make fmt` | Ruff fix and format |
| `make up`, `make down`, `make logs` | Start the stack in Docker (build included), stop it, follow logs |
| `make db-up` | Postgres from compose, with the dev override |
| `make migrate`, `make revision m="..."` | Run Alembic, or autogenerate a revision |
| `make login` | Log the second account in locally and save the session |
| `make login-docker` | The same inside the compose image and volume |

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
| `USERBOT_SESSION_PATH` | yes | | Where the Telethon session file is stored; Telethon appends `.session`, and the parent directory is created if needed. The file is a secret |
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
(the userbot opens the session path; nothing opens the media directory yet), it is relative to the
current working directory of the process. Inside the
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
migration and the repositories, and for the userbot: the filters, the handler, the client and
startup logic, the login flow, the intake on real PostgreSQL and the check that the userbot never
sends. Telegram is always faked: no test connects to it. All tests use fake values only.

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
