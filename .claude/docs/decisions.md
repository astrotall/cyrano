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
| Timestamps stored in UTC; `TIMEZONE` applied only at the edges | Avoids offset bugs in stored data |

## Open questions

| Question | Ticket |
| --- | --- |
| How does the worker send through the userbot session? The session volume is mounted into `app` only | CYR-17 |
| How do Alembic migrations run (on app start, a separate step, a compose service) and where do they live? | CYR-9 |
| How does the worker reconnect to Redis after a Redis restart? | CYR-17 |
| Scrubbing of free-text logs and coverage tooling | CYR-28 |
| Explicit compose project name (today it defaults to the directory name) | open, no ticket |
| `uv_build` is pinned `>=0.12.4,<0.13.0` in `pyproject.toml`; bump it when uv reaches 0.13 | open, no ticket |
| The full status transition matrix (only the transitions in [contact-lifecycle.md](contact-lifecycle.md) are decided) | CYR-9 |
| Debounce mechanism and window length | CYR-17 |
| How many last messages go into the prompt | CYR-16 |
| Where the layered prompt builder lives (`llm`, `services` or `prompts`) | CYR-16 |
| How stop-triggers are detected (lists, model output, both), where each is evaluated, and how model confidence is obtained and thresholded | CYR-20 |
| Numbers: rate limits, humanized delay, quiet hours window | CYR-22, CYR-18 |
| Numbers: limits on proactive messages beyond two unanswered | CYR-23 |
| May `bot`, `userbot` and `worker` import each other, or only `services`? | open, no ticket |
| Which hooks CYR-29 installs and which tool manages them | CYR-29 |
