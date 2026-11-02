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
| `docker-compose.override.yml` bind-mounts `./data/media` over `/data/media` in `app` and `worker`; production (`docker-compose.yml` alone) keeps the named `media` volume | Collected photos are visible in the project folder without copying them out of the Docker VM |
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
| A bounded in-memory queue (1000) carries the events; a full queue drops the new event and logs a warning with counters | Intake must never block or fail because a consumer is slow or missing. The profile collector consumes it (CYR-11) |
| A contact is registered on any private message from a real user, media-only messages included; the text is the caption or an empty string; media is not downloaded | The owner has to see everyone who wrote. `messages.text` allows an empty string, so no placeholder is needed. Downloads belong to a later ticket |
| NUL bytes are removed from text, username and display name before any repository call | PostgreSQL rejects NUL in text and JSONB; otherwise one such message would fail every redelivery of itself |
| Two simultaneous first messages are resolved by get-after-conflict: the loser of `create` reads the contact by `tg_id` and continues as for a known sender | The unique `tg_id` already arbitrates; no extra lock or retry loop. Exactly one call created the contact, so exactly one event is published |
| Catch-up on every start: the client is built with `catch_up=True` and `catch_up()` is called after connect | Messages that arrived while the app was down are processed. Calling `catch_up()` alone would not load the update state saved in the session. Idempotency makes replays safe |
| The handler is registered before the client connects | No update can arrive at a client without a handler |
| The app exits at once with "run `make login`" when the session file is missing, before any client exists; an unauthorized session and a 30 s connect timeout are readable fatal errors too; normal startup never prompts | A restart loop must never hang, prompt for input or touch the network without a session. The restart policy handles the rest |
| An unexpected disconnect ends the process with an error | Telethon already retries; when it gives up, a restart is the honest recovery, and the restart policy provides it |
| The handler retries the intake call: 3 attempts in total, 1 s then 2 s apart, for any exception except `asyncio.CancelledError`; after the last attempt it logs an error with the class name and counters and drops the message | A transient failure (a database blip) should not lose a message. Intake is idempotent (message append on `(contact_id, tg_message_id)`, contact creation by get-after-conflict), so a repeat is safe. Updates are sequential, so a retry delays the next update by a few seconds; accepted. Limit: a failure after the contact was created but before its event was published still loses the event (events are best-effort) |
| SIGTERM and SIGINT during startup end it at once: the connect and the catch-up are each raced against the stop event, the 30 s connect timeout stays for the real timeout case | The connect timeout is longer than Docker's 10 s grace period, so waiting it out meant SIGKILL. A stop request must give a clean shutdown (handler removed, client closed, engine disposed, exit 0) in about a second |
| The profile collector consumes `NewContactEvent` first; the card (CYR-12) starts from `ContactReadyEvent` | Answers the earlier open question about the order of consumers. The card must show nick, bio, photo and the matching-bot text, so something has to collect them before the card, and the card must not poll the database |
| `ContactReadyEvent(contact_id)` means "collection finished, successfully or not, the card may be shown" and is published in a `finally` for every attempted contact | The card must never wait forever for a profile that Telegram refuses to give. The consumer shows what is there. Nothing consumes it yet (CYR-12); it is a second in-memory queue of the same generic class as the first |
| The in-memory event queue is generic over the event type (`InMemoryContactEvents[T]`, `ContactEventPublisher[T]`) | Two event types, one implementation, no `Any` |
| Collection is strictly sequential: one event, one contact at a time | Gentle on Telegram limits (account limits and bans are a main risk); the owner likes profiles by hand, so the volume is low and parallelism buys nothing |
| The three parts (Telegram profile, photo, Leomatch lookup) run independently, each in its own `try/except`, under one 30 s budget per contact (`asyncio.wait_for`) | A privacy-restricted photo or a missing matching-bot chat must not cost the owner the bio. A hanging call must not block every later contact. What was gathered before the budget ran out is still stored |
| The raw Leomatch text is always stored; name, age, city and about are filled only when the text matches the observed card format | Parts of the format are still unobserved (see open questions). The owner can always read the raw text on the card, whatever the parser made of it |
| `collected_at` is set only if the Telegram profile part succeeded; a failed photo or Leomatch part does not block it | Without it the startup sweep retries the contact. Bio and nick are the core of the card; the photo and the matching-bot text are extras that are not worth an endless retry |
| A startup sweep processes contacts in status `new` without `collected_at` before the first event | Events are best-effort and in memory; the database is the source of truth. The sweep repairs lost events and retries failed contacts |
| `ContactRepository.update_identity` refreshes `username` and `display_name` from the fetched profile | CYR-10 writes them once from the first message, which can be stale or missing; Telegram is the source of truth |
| The photo is saved as `profiles/<internal contact id>.jpg` under `MEDIA_DIR` through a `.part` file, and the profile stores the path relative to `MEDIA_DIR` | A name made from the Telegram id or a name would put personal data into file listings and backups; a relative path survives a changed `MEDIA_DIR` and the move between the host and the container; the rename leaves no half-downloaded photo |
| The collector never sleeps or retries on a flood wait: the adapter maps every Telegram failure to `ProfileSourceError` and that part fails | The sweep or the next event retries later; a sleeping collector would hold the whole queue. Telethon itself still waits out short flood waits inside one call (its `flood_sleep_threshold`, 60 s by default); the 30 s budget cancels such a wait |
| The matching-bot lookup is read-only: server-side search for the username (bare and with `@`, at most 5 results each), then a local scan of the last 100 messages; the newest message that passes the strict check is the match message | Searching is cheap and precise when it works; the scan is the fallback, and the only method without a username. No write method is used and nothing is marked as read |
| The strict match check accepts a user-mention entity with the contact's Telegram id, a `tg://user?id=` link, a `t.me/<username>` link or a standalone `@username`; comparison is case-insensitive and word-boundary aware; evidence by id beats evidence by username, and a message that names another user id never matches by username | `ann` must never match `joanna`, `ann_k` or `ann2`. A match message for someone else is worse than none: the owner would see a stranger's text on the card |
| The card is looked for at most 2 positions before the match message; outgoing messages and messages without text are skipped; the first incoming text that does not look like a match message is the card, and the walk stops there; a message with a user mention, a `tg://user` link or a `t.me/` link ends the walk without a card | Two chain shapes exist (card, like, match and card, match). Never take two cards, never take someone else's |
| Observed on a real match (CYR-30). Card message: a header paragraph ending with a colon, a blank line, then `Name, age, City` with no description. Match message: a congratulation, a blank line, then `Начинай общаться 👉 <name>` where the name is a user-link entity and the person has no public username | Replaces the format that CYR-11 assumed. The texts are the matching bot's own wording, with an invented name in fixtures |
| The parser skips a first paragraph that ends with a colon when at least one more non-empty paragraph follows; it does not whitelist the header wording. The description is optional (`about` stays empty without it); `Name, age`, `Name, age, City` and `Name, age, City <dash> description` are accepted, the dash being a hyphen, en dash or em dash with whitespace on both sides, so `Санкт-Петербург` is not split. Age 18 to 99, emoji in the name allowed. A text that does not match keeps only `raw_text` | The header wording may vary with the bot's version; a colon plus a following paragraph is the structure that was observed. Requiring whitespace around the dash is what keeps hyphenated cities whole |
| The match message is found by the user-link entity with the contact's Telegram id even when the contact has no username; confirmed live | Resolves the earlier worry that a contact without a username could not be matched. Evidence by id was already the strongest evidence |
| The card photo is stored separately from the profile photo: `profiles/<internal contact id>-card.jpg` through a `.part` file, path relative to `MEDIA_DIR` in `leomatch.photo`; the Telegram profile photo stays in `photo`. It is a read-only `download_media` of the card message (a new read member of the client Protocol, no new request class) | The owner sees the picture the matching bot showed next to the card text, which may differ from the profile photo. Reading a message's media sends nothing and marks nothing as read |
| The card photo is its own collection part (`card_photo`): a failure never blocks collection or touches another part, and a retry that fails or finds no card photo keeps `leomatch.photo` stored by an earlier run | Same reasoning as for the other parts; a photo gathered once is not thrown away by a later failure |
| `LeomatchSource.find_texts` returns a `LeomatchLookup` (the labelled texts and an optional `CardPhoto` handle) instead of a bare list | The collector learns from one lookup whether the card has a photo and can download it, without a second search in the matching-bot chat |
| New setting `LEOMATCH_BOT_USERNAME`, default `leomatchbot`; timeouts and limits are constants, not settings | The default is a guess to check in Telegram. Limits change with the code, not per deployment |
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
| Which hooks CYR-29 installs and which tool manages them | CYR-29 |
| Leomatch, still unobserved: a card with a description (the dash form `Name, age, City <dash> description` is a guess), the other chain shape (the owner liked the person first: card, like, match), and the behaviour of Telegram's server-side search for `t.me/<username>` links. A late match may not have its card next to it; then only the match text is stored. Also check that the default `LEOMATCH_BOT_USERNAME` is the right bot | CYR-11 follow-up, next real matches |
