# Architecture

State as of CYR-16: the skeleton, the storage layer, the userbot intake, the profile collection, the
control bot with the contact card, the LLM client, the persona with the style examples and the
layered reply prompt with the reply generator exist. The LLM client, the persona, the examples and
the reply generator have no consumer in the running app: only the `llm-check` and `reply-preview`
commands use them. Anything else is marked **planned** with its ticket.
Product and decisions: [CLAUDE.md](../../CLAUDE.md). Reasons: [decisions.md](decisions.md).

## Layers

Source lives in `src/cyrano/`.

| Package or module | Role | State |
| --- | --- | --- |
| `userbot/` | The only package that imports Telethon: client, session and login, event filters, the incoming-message handler, the listener lifecycle. Receives private messages on the second account and converts events into plain objects. Also holds the read-only adapters of the profile collection: `profile_source.py` (bio, names, photo) and `leomatch_source.py` with `leomatch_match.py` (the matching-bot chat). Planned: the actual Telegram send behind the sender (CYR-19) | exists for listening and registration (CYR-10) and for reading profiles (CYR-11); it sends nothing |
| `bot/` | The only package that imports aiogram: the owner-only control bot. `app.py` (`ControlBot`: bot, dispatcher, polling, stop), `middleware.py` (owner-only), `handlers.py` (`/start`, the card buttons), `rendering.py` (pure card text, keyboard, decision), `callbacks.py` (callback data), `sender.py` (`TelegramCardSender`), `messages.py` (every Russian text), `errors.py` (readable fatal errors). Planned: notifications of stop-triggers and the rest of the panel (CYR-20, CYR-26) | exists for the card and its two buttons (CYR-12) |
| `llm/` | The only package that imports httpx and the only one that knows the provider: `deepseek.py` (`DeepSeekClient`, `StepConfig`), `transport.py` (`RetryingTransport`), `factory.py` (`LLMClientFactory`), `wire.py` (the response fields it reads), `json_reply.py` (JSON extraction and validation problems), `prompts.py` (the texts it adds for JSON), `constants.py` (every number and string). See [LLM client](#llm-client-cyr-13) | exists (CYR-13); not wired into `main.py` yet, the first consumers are CYR-16, CYR-17 and CYR-25 |
| `db/` | SQLAlchemy models (`models.py`), engine and session factory (`session.py`), repositories (`repositories.py`; CYR-14 added `SqlPersonaRepository` and `SqlStyleExampleRepository`) | exists; called by the intake (CYR-10) and by the import commands (CYR-14) |
| `services/` | Business logic shared by userbot, bot and worker: contact lifecycle, sender, reply pipeline. `repositories.py` holds the repository Protocols, `intake.py` the `IntakeService`, `sanitize.py` the NUL-byte removal, `contact_events.py` the generic `ContactEventPublisher[T]` and `ContactEventSubscriber[T]` Protocols and the in-memory queue; CYR-11 added `profile_source.py` (the `ProfileSource` and `LeomatchSource` Protocols and `ProfileSourceError`), `leomatch.py` (the card parser) and `profile_collector.py` (`ProfileCollector`); CYR-12 added `card_sender.py` (the `CardSender` Protocol), `card_builder.py` (assembles the `ContactCard`), `card_announcer.py` (`CardAnnouncer`) and `card_actions.py` (`CardActions`); CYR-13 added `llm_client.py` (the `LLMClient` Protocol) and `llm_check.py` (`check_llm`, the request behind `llm-check`); CYR-14 added `style_files.py` (reading and validating the two import files), `style_import.py` (`PersonaImporter`, `ExamplesImporter`), `persona_service.py` (`PersonaService`) and `style_examples.py` (`ExampleSelector`, `collect_example_stats`), and the `PersonaRepository` and `StyleExampleRepository` Protocols in `repositories.py`; CYR-16 added `contact_facts.py` (`build_contact_facts`), `prompt_builder.py` (`PromptBuilder`), `reply_postprocess.py` (`clean_reply`) and `reply_generator.py` (`ReplyGenerator`); see [Reply generation](#reply-generation-cyr-16) | Protocols, intake, sanitization, events (CYR-10), the profile collector (CYR-11), the card announcer and actions (CYR-12), the LLM Protocol (CYR-13) and the persona and example services (CYR-14) and the prompt builder and reply generator (CYR-16) exist; the rest is planned from CYR-15 on |
| `worker/` | Arq worker: `settings.py` holds `WorkerSettings`, `startup`, `shutdown`, a placeholder `noop` job (Arq refuses to start a worker with no registered functions) | skeleton works; real jobs planned CYR-17 |
| `config.py` | `Settings` (pydantic-settings), `load_settings`, `ConfigError` | exists |
| `logging.py` | JSON-lines formatter, `build_log_config`, `configure_logging` | exists |
| `health.py` | Heartbeat file, `is_alive`, `python -m cyrano.health` | exists |
| `main.py`, `__main__.py` | App entrypoint (composition root), the `login`, `llm-check`, `persona`, `examples` and `reply-preview` subcommands, signal handling, heartbeat task | exists; the userbot is plugged in (CYR-10), the collector (CYR-11), the control bot and the announcer (CYR-12); `llm-check` builds the LLM factory (CYR-13); the persona and examples commands build the two repositories and the importers (CYR-14); `reply-preview` builds the repositories, the selector, the `PromptBuilder`, the `reply` client and the `ReplyGenerator` (CYR-16); `run` builds neither the LLM client nor the persona, example and reply services yet |
| `style_cli.py` | The `persona` and `examples` commands: the argparse definitions, `StyleRequest` and `StyleCommands`, which receive their importers, service and repository by injection and print counts only. `main.py` builds the concrete objects | exists (CYR-14); see [Persona and style examples](#persona-and-style-examples-cyr-14) |
| `convert_cli.py` | The `examples convert` command (CYR-34): the argparse definition, `ConvertRequest`, the report printing. `main.py` dispatches it **before** `load_settings`, so it needs no `Settings`, `.env`, database or network | exists (CYR-34); see [Chat export converter](#chat-export-converter-cyr-34) |
| `reply_cli.py` | The `reply-preview` command (CYR-16): the argparse definition, `ReplyPreviewRequest`, `ReplyPreviewCommand`, which receives the `ReplyGenerator` by injection and prints the reply, one stats line and one-line errors. `main.py` builds the concrete objects | exists (CYR-16); see [Reply generation](#reply-generation-cyr-16) |
| `domain/` | Leaf package, imports only pydantic and the standard library: enums, status transition rules, domain errors (the LLM errors, the import validation errors and the reply errors among them), Pydantic models the layers exchange (`reply.py`: `ContactFacts`, `BuiltPrompt`, `GeneratedReply` and the reply limits; `llm.py`: `LLMMessage`, `LLMUsage`, `LLMResult`; `style.py`: `Persona`, `Turn`, `StyleExample`, `StyleExampleCreate`, the persona and example limits, the content hash; `chat_export.py`: the converter's limits, `SkipReason`, `RedactionKind`, `ConvertOptions`, `ConvertReport`; `ChatExportError`) | exists |
| `prompts/` | Prompt text only: `texts.py` (headers, instructions, labels, the dialogue boundary token) and `render.py` (pure functions that render the system and the user message). No calls, no IO; imports only `domain` | exists (CYR-16). The builder itself, with its budget logic, lives in `services/prompt_builder.py`. Planned in the same package or `config`: trigger lists (CYR-20) |

`tests/` mirrors the packages (`tests/userbot`, `tests/bot`, `tests/llm`, `tests/domain`,
`tests/db`, `tests/prompts`, `tests/services`, `tests/worker`, and `tests/cli` for `style_cli.py`,
`convert_cli.py` and `reply_cli.py`); the skeleton packages have empty test packages.
`tests/userbot/fakes.py` holds the fake Telethon client, event and sender; `tests/llm/fakes.py` the scripted `httpx.MockTransport` handlers.

## Import direction

Enforced by review. `tests/test_import_direction.py` also checks the part that exists: `domain`
imports only pydantic, the standard library and itself; `prompts` imports only the standard library
and `domain`; `db` imports neither `services` nor `llm`; `services` imports neither `db`, `userbot`
nor `bot`, nor Telethon, aiogram or httpx, and not `llm`; only `services` and `prompts` import
`prompts`; `userbot` imports
none of `db`, `llm`, `bot`; `bot` imports none of `db`, `userbot`, `llm`; `llm` imports none of `services`, `db`, `userbot`,
`bot`; only the `userbot` package imports `telethon`, only the `bot` package imports `aiogram`
and only the `llm` package imports `httpx`; outside `llm` only `main.py` imports `cyrano.llm`, and
no module outside `llm` and `config.py` mentions the provider by name.

1. `bot`, `userbot` and `worker` may import `services`.
2. `services` reach `llm` and `db` only through interfaces defined in `services`. The concrete
   `llm` and `db` code satisfies those interfaces without importing `services`. For `db` this is
   fact: `services/repositories.py` defines `ContactRepository`, `MessageRepository` and
   `ModeRepository`, `db/repositories.py` satisfies them structurally, and mypy checks that in
   `tests/db/test_session.py`.
3. `llm` and `db` never import `services` or each other.
4. `config`, `domain` and `prompts` are leaves: they import no application layer (`prompts` imports
   `domain` only). `services` use `prompts`; no other layer needs it. Today `config` also imports `health` for the default heartbeat path (`health` is
   stdlib-only), so `config` is not a pure leaf until that default moves.
5. `main` is the composition root: the only place that builds concrete objects and passes them
   to the layers.
6. Telethon is imported only inside `userbot/`. The rest of the code, tests of other layers
   included, sees it through the Protocols in `userbot/protocols.py`.
7. aiogram is imported only inside `bot/`. `services` reach the owner through the `CardSender`
   Protocol and receive the owner's decisions through `CardActions`; `bot` calls `CardActions` and
   implements `CardSender`, and `main` joins them.
8. httpx is imported only inside `llm/`. Consumers import the `LLMClient` Protocol from
   `services/llm_client.py` and the models and errors from `domain`; no httpx exception and no
   provider class crosses the package boundary.

Never import upward. Whether `bot`, `userbot` and `worker` may import each other is an open
question in [decisions.md](decisions.md).

## Where does this file go?

| You are writing | It goes to |
| --- | --- |
| A rule about contacts, statuses, when to reply, the sender and its guard | `services` |
| Telethon events, sessions, converting a Telethon entity into a plain object | `userbot` |
| aiogram handlers, keyboards, card formatting | `bot` |
| HTTP calls to the LLM provider and parsing its response | `llm` |
| The interface consumers use to talk to a language model | `services/llm_client.py` |
| LLM messages, results, usage and errors | `domain` |
| SQLAlchemy models, repository implementations, session handling | `db` |
| A repository interface | `services/repositories.py` |
| Arq job functions and worker settings | `worker` |
| A data type or rule used by several layers | `domain` |
| Prompt text and the pure functions that render it | `prompts` |
| Assembling a prompt from stored data, trimming it to a budget, cleaning a model reply | `services` (`prompt_builder.py`, `reply_postprocess.py`, `reply_generator.py`) |
| Trigger lists | config or a dedicated module (CYR-20) |
| Parsing or validating a persona or examples file, choosing the examples for a mode | `services` (`style_files.py`, `style_examples.py`) |
| A command-line subcommand and its output | an injected class in a top-level module like `style_cli.py`; `main.py` parses, builds and dispatches |
| An environment-driven setting | `config.py` and the README Settings section, together; `.env.example` too when the variable is required |
| Wiring concrete objects together | `main.py` |
| Alembic migrations | `migrations/versions/` at the repository root |

## Process model

Two Python processes, two stores, one external API each for Telegram and the LLM.

```
 Telegram <--> app process (python -m cyrano)       worker process (arq)
               userbot + control bot (exist)         delayed jobs (planned)
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
  touches the heartbeat file, connects the userbot, registers the handler, catches up, then starts the
  profile collector, the card announcer and the control bot's polling as background tasks (only once
  the userbot is connected, because the collector reads through the same client and the announcer
  would otherwise show cards before the intake runs) and waits for SIGTERM or SIGINT; a signal during
  connect or catch-up ends the start at once, and then none of the three is started (see
  [Userbot](#userbot-cyr-10)). A lost connection that Telethon cannot restore ends the process with an
  error, and the restart policy starts it again; so does a control bot that stops polling (see
  [Control bot](#control-bot-cyr-12)). On shutdown it cancels the card announcer, stops polling and
  closes the bot session, cancels the collector, removes the handler, disconnects the userbot, stops
  the heartbeat, removes the file and disposes the engine; planned: close the Redis connections there.
  It has the `userbot_session` volume.
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
card (with its photo) and the match message; the collector reads them together with nick, name,
bio and photo for the owner's card. The system never sends anything to the matching bot. The steps, the stored
structure and the semantics of `collected_at` are in
[contact-lifecycle.md](contact-lifecycle.md#profile-collection-cyr-11).

## Userbot (CYR-10)

`src/cyrano/userbot/`. The one package that imports Telethon. It only listens and stores: no
module calls `send_*`, `mark_read`, `send_read_acknowledge` or any other write method.

| Module | Role |
| --- | --- |
| `protocols.py` | The small surface of Telethon that the code uses, as Protocols: `SenderLike`, `IncomingEvent`, `UserbotClient`, `LoginClient`. `UserbotClient` has `connect`, `disconnect`, `is_user_authorized`, `catch_up`, `add_event_handler`, `remove_event_handler`, `disconnected` and, since CYR-11, only the read members `get_entity`, `download_profile_photo`, `download_media`, `iter_messages` and `__call__` (typed to accept only the full-user request); no write method; a test enforces that list |
| `filters.py` | Pure functions: `event_rejection(event)` and `sender_rejection(sender)` return a `Rejection` or `None` |
| `handler.py` | `IncomingHandler`: registers one `events.NewMessage(incoming=True)` handler, converts an event into `IncomingMessage`, calls the intake, catches and counts failures |
| `client.py` | Builds the `TelegramClient` from `Settings`, session file naming and mode, `connect_authorized` with the timeout, `disconnect_client` |
| `runner.py` | `Userbot`: start, run until stopped or disconnected, stop |
| `login.py` | The interactive login flow with injected prompts |
| `errors.py` | `UserbotError` and subclasses; their messages are meant to be read by the owner |
| `profile_source.py` | `TelethonProfileSource`, the `ProfileSource` adapter: the user from the session cache (`get_entity`), the bio from `GetFullUserRequest`, the big current profile photo. Every Telethon failure becomes `ProfileSourceError` carrying the class name; it never sleeps on a flood wait |
| `leomatch_source.py` | `TelethonLeomatchSource`, the `LeomatchSource` adapter: resolves the chat with `LEOMATCH_BOT_USERNAME` (an unresolvable chat gives an empty lookup and a DEBUG line), searches, scans, groups the messages before the match into positions (an album is one position), walks back for the card and hands out access to the card photos (`TelethonCardPhotos`, a read-only `download_media` of one message of the card position); `to_chat_message` turns Telethon messages and entities into the plain `ChatMessage`, including the text under each user-link entity |
| `leomatch_match.py` | Pure: `ChatMessage`, `UserLinkText`, `match_evidence` (the strict match check), `linked_name` (the text under the link to the contact), `looks_like_match_message`, `normalize_username` |

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
`ContactReadyEvent` (published by the collector, consumed by the card announcer, CYR-12). A full queue drops the new event and logs
a warning with counters; `publish` never raises. The queue lives in one process and is lost on
restart, which is fine because events are best-effort and the database is the source of truth.

## Profile collection (CYR-11)

`services/profile_collector.py`: `ProfileCollector(contacts, profiles, leomatch, new_contacts,
ready, media_dir)`. It depends on the `ContactRepository` Protocol, on the two source Protocols in
`services/profile_source.py` and on the event Protocols, never on Telethon or SQLAlchemy.

- **Protocols.** `ProfileSource.fetch_profile(tg_id)` returns a `TelegramProfile` (username,
  display name, bio); `ProfileSource.download_photo(tg_id, destination)` returns whether a photo was
  saved; `LeomatchSource.find_texts(tg_id, username)` returns a `LeomatchLookup`: `card_text`,
  `match_text` and `match_name` (the text under the link to the contact in the match message), each
  `None` when not found, and `card_photos`, a `CardPhotos` with the `count` and a read-only
  `download(index, destination)` when the card position has photos, otherwise `None`. The lookup
  works on positions: consecutive messages with the same `grouped_id` (an album) are one position;
  the rules are in [contact-lifecycle.md](contact-lifecycle.md#the-card-position). Every adapter failure (flood wait, privacy
  restriction, not found, timeout) is one `ProfileSourceError`.
- **Parser.** `services/leomatch.py`: `parse_leomatch_text` never raises and fills name, age, city
  and about only for the observed card format (a header paragraph ending with a colon is skipped;
  the description is optional); `build_leomatch_profile` assembles the stored
  `LeomatchProfile` from the card text, the match text and the link name; the match text is stored apart and never parsed. Linear time: it splits strings and uses one simple
  pattern, no nested quantifiers.
- **Process.** Started by `main.run` after the userbot connected and caught up, cancelled first on
  shutdown, before the client disconnects. A collector that crashes is logged once (class name) and
  does not stop the intake.
- **Media directory.** `MEDIA_DIR/profiles/<internal contact id>.jpg` for the Telegram profile photo and
  `<internal contact id>-card-<n>.jpg` (n from 1, up to 5) for the Leomatch card photos, created on
  demand; `<id>.part.jpg` and `<id>-card-<n>.part.jpg` exist only during a download. The `media` compose volume is mounted into `app`
  and `worker`; the dev override replaces it with a bind mount of `./data/media`.
- **Logger.** `cyrano.services.profile_collector` (INFO: contact id and which parts produced data;
  WARNING: contact id, part name and error class of a failed part; ERROR: error class and counter
  of a failed event), `cyrano.userbot.leomatch_source` (DEBUG only).

## Control bot (CYR-12)

`src/cyrano/bot/`. The only package that imports aiogram (3.31 at the time of CYR-12; it ships type
information, so mypy needs no override). It runs inside the app process on the same event loop as
the userbot, by long polling.

| Module | Role |
| --- | --- |
| `app.py` | `build_dispatcher(owner_id, actions)` and `ControlBot`: builds the `Bot` from `BOT_TOKEN` (HTML parse mode, link previews off), the dispatcher, and the `TelegramCardSender`; `poll()` runs `start_polling(handle_signals=False, close_bot_session=False)`; `stop()` stops polling (bounded) and closes the session |
| `middleware.py` | `OwnerOnlyMiddleware`, an outer middleware on every update: a user other than `OWNER_TELEGRAM_ID`, or no user at all, is dropped silently. No reply, no database access, only a counter (DEBUG, no ids) |
| `handlers.py` | `build_router(actions)`: `/start` (private chat only) and the card button handler |
| `rendering.py` | Pure functions: `render_card`, `card_keyboard`, `append_decision`, `alert_text`. The matching-bot section comes only from the card text (a short line when it was not found; a fallback name from the match message is shown as the name). Text is HTML-escaped; the card is assembled from sections within `CARD_TEXT_BUDGET` (3500) so it stays below Telegram's 4096 whatever the input |
| `callbacks.py` | `CardCallback` (`card:<action>:<contact id>:<mode id>`, a few bytes against Telegram's 64) |
| `sender.py` | `TelegramCardSender`, the `CardSender` implementation. The owner id is a constructor argument taken from `Settings` and the only public method is `send_card`, so no caller can address another chat |
| `messages.py` | Every Russian text the owner sees. Texts and constants live here, not in the handlers |
| `errors.py` | `ControlBotError`, `BotTokenRejected`, `PollingFailed`; their messages are meant to be read by the owner |

- **Sending.** Order: the photos, then the card text with the keyboard. The profile photo comes
  first, then all card photos, as ONE media group (a single photo uses `send_photo`; with at most
  1 + 5 photos the group stays below Telegram's 10 items). The caption is on the first item only,
  because Telegram shows only that one: «Фото профиля и анкеты» when both kinds are sent, «Фото
  профиля» or «Фото анкеты» otherwise. A photo file that is missing, empty, unreadable or outside `MEDIA_DIR` is
  skipped and counted. `TelegramRetryAfter` is waited for (at most `MAX_RETRY_AFTER_SECONDS`, 60) and
  retried once; a second one, or a longer wait, gives up. `Forbidden` (the owner never pressed
  Start), "chat not found", network errors and any other Telegram API error return `False`: the
  contact stays `new` and the sweep retries. One deliberate exception: a `BadRequest` on the photo
  step that is not "chat not found" (for example a photo Telegram rejects) skips the photos and
  still sends the text card, otherwise one bad file would block a contact forever.
- **Callback handling.** `CardActions` does the work; the handler always answers the callback query
  (an exception becomes a generic alert), edits the card on `applied` and `already_applied`,
  otherwise removes the keyboard when the contact is no longer `pending`, and treats a card it cannot
  edit as answered only. Details: [contact-lifecycle.md](contact-lifecycle.md#card-announcement-cyr-12).
- **Polling and signals.** Polling runs as a task with `handle_signals=False`: our own SIGTERM and
  SIGINT handling in `main` stays in charge. An unexpected end of polling, a rejected token and any
  exception from `start_polling` stop the app with a readable error and a non-zero exit (the restart
  policy restarts it). Known limit: aiogram retries failing `getUpdates` calls itself with backoff and
  never gives up, so a token revoked while running is retried forever; only a start-time rejection is
  fatal. The owner not having pressed Start is not fatal: the sends fail softly.
- **Shutdown order.** Cancel the card announcer, stop polling and close the bot session, cancel the
  polling task, then the existing steps: collector, handler removal, userbot disconnect, heartbeat,
  engine. The announcer goes first so no card is being sent while polling stops.
- **Logger.** `cyrano.bot` (INFO: stopped), `cyrano.bot.handlers` (INFO: contact id, action and outcome
  of a button press; WARNING: error class; DEBUG: a card that could not be edited),
  `cyrano.bot.sender` (WARNING: error class and counters), `cyrano.bot.middleware` (DEBUG),
  `cyrano.services.card_announcer` (INFO: contact id of an announced card; WARNING: counters and
  error class, at most one per sweep).

## LLM client (CYR-13)

`src/cyrano/llm/` plus `services/llm_client.py` and `domain/llm.py`. One interface for reply
generation (CYR-16, CYR-17) and summary extraction (CYR-25), DeepSeek the only provider. Nothing in
this ticket sends a message or touches a contact, and the app process does not build the client yet:
only `python -m cyrano llm-check` does.

| Where | What |
| --- | --- |
| `domain/enums.py` | `Role` (`system`, `user`, `assistant`) and `LLMStep` (`reply`, `summary`) |
| `domain/llm.py` | `LLMMessage(role, content)`, `LLMUsage(input_tokens, output_tokens, cache_hit_tokens)`, `LLMResult(text, usage, truncated)`. The message class is `LLMMessage` because `domain.models.Message` is the stored chat message |
| `domain/errors.py` | `LLMError(message, status_code)` under `DomainError`, and `LLMAuthError` (401, 403), `LLMRequestError` (other 4xx except 429, invalid input), `LLMRateLimitError` (429 after the retries, or a `Retry-After` beyond the cap), `LLMUnavailableError` (5xx or a network failure after the retries), `LLMInvalidResponseError` (empty reply, unexpected body shape, no valid JSON after the attempts) |
| `services/llm_client.py` | The `LLMClient` Protocol: `complete(messages, *, temperature, max_tokens)` and `complete_json(messages, schema, *, temperature, max_tokens)` |
| `llm/deepseek.py` | `DeepSeekClient`, bound to a step (model, default temperature, default max tokens); satisfies the Protocol without importing `services` |
| `llm/transport.py` | `RetryingTransport`: every request goes through it |
| `llm/factory.py` | `LLMClientFactory(settings)` builds the shared `httpx.AsyncClient` once; `get(LLMStep.REPLY)` returns a bound client; `aclose()` closes the http client explicitly. Nothing is built at import time |

- **Requests.** `POST {DEEPSEEK_BASE_URL}/chat/completions` with the OpenAI-compatible body: `model`,
  `messages`, `temperature`, `max_tokens` (always set: the per-call value, else the step default),
  `stream: false` and `thinking`. `Authorization: Bearer <DEEPSEEK_API_KEY>` is set once on the shared
  http client and nowhere else. An empty message list, a list without a `user` message, a temperature
  outside 0..2 and a non-positive `max_tokens` are `LLMRequestError` before any HTTP call.
- **Thinking.** The documented request parameter is `thinking: {"type": "enabled" | "disabled"}`, and
  DeepSeek enables thinking when it is omitted. The client always sends it: `disabled` unless
  `DEEPSEEK_THINKING=true`. A chat reply therefore spends no tokens on reasoning and `temperature`
  has effect (the provider ignores it in thinking mode).
- **Reply.** `text` is `choices[0].message.content`; `truncated` is `finish_reason == "length"`;
  `usage` comes from `prompt_tokens`, `completion_tokens` and `prompt_cache_hit_tokens` (absent means
  `None`). A blank reply from `complete` is `LLMInvalidResponseError`; so is a body that is not JSON,
  has no choice or has no usage.
- **`complete_json`.** The client inserts one system message with the JSON Schema of the model after
  the caller's leading system messages and asks for a single JSON object; it also sends
  `response_format: {"type": "json_object"}`, which the provider documents together with the
  requirement that the prompt mentions JSON (the schema message does). The JSON is read from a fenced
  block anywhere in the reply (` ```json ` or a bare fence), otherwise from the whole reply, and
  validated with `model_validate_json`, so a partially valid object is never returned. An invalid
  reply goes back as an assistant turn followed by a user turn that lists the problems as
  `- <field path>: <message>` (never input values) and, for a truncated reply, asks for a shorter
  output. A blank reply is retried unchanged; the provider documents that JSON mode can return only
  spaces. The first call plus `LLM_JSON_MAX_RETRIES` (2) retries are made, the two kinds of failure
  sharing that budget; after that `LLMInvalidResponseError` with the attempt count only. The invalid
  reply stays inside the package: it is not in the exception (which is raised with no cause), not in a log
  line and not in the exception chain. The prompt texts of this step live in `llm/prompts.py`.
- **Transport rules** (`RetryingTransport`, constants in `llm/constants.py`):
  - Network errors, 429 and 5xx are retried up to 3 times with exponential backoff, base 1 s, cap
    30 s, jitter lowering each delay by up to half (`delay = ceiling * (1 - 0.5 * jitter)`).
  - `Retry-After` (seconds or an HTTP date) replaces the backoff on a retried status. If it asks for
    more than the 30 s cap the call fails at once (`LLMRateLimitError` for 429, `LLMUnavailableError`
    for 5xx). An unreadable value falls back to the backoff.
  - Other 4xx are never retried: 401 and 403 give `LLMAuthError`, the rest `LLMRequestError` (402,
    "insufficient balance", is one of them). Any other non-2xx status is `LLMInvalidResponseError`.
  - Every attempt runs under a hard deadline of 60 s enforced with `asyncio.timeout`, because a
    provider can hold a connection open without answering. A missed deadline counts as a network
    error. The http client also has connect, read, write and pool timeouts.
  - The sleep, the jitter source and the clock are constructor parameters, so tests never wait.
  - No `httpx` exception leaves the package: they are caught and re-raised as domain errors with
    `from None`. Exceptions and logs carry the status code or the exception class only, never a
    response body, a message text or the key.
- **Per-step settings** (all optional; the table is under [Configuration](#configuration)):
  `LLM_REPLY_MODEL` and `LLM_SUMMARY_MODEL` fall back to `DEEPSEEK_MODEL`; temperatures default to 1.0
  (reply) and 0.2 (summary); max tokens to 400 and 1500. Invalid values give the usual readable
  `ConfigError` without echoing them.
- **Logging.** Logger `cyrano.llm.deepseek`: one INFO line per HTTP call with `step`, `model`,
  `input_tokens`, `output_tokens`, `cache_hit_tokens` (only if the provider sent it), `duration_ms` and
  `retries`; WARNING `llm json reply rejected` with `step`, `attempt`, `max_attempts`, `reason`,
  `truncated` and `problem_count`. Logger `cyrano.llm.transport`: WARNING `llm request will be
  retried` with the retry number, the status or the exception class and the delay. Prompts, replies,
  response bodies and the key are never logged at any level; a test captures all log output and
  asserts it (S9).
- **`llm-check`.** `python -m cyrano llm-check` (`make llm-check`) builds the `reply` client from
  `Settings`, sends the system message "reply with the single word OK" and the user message "ping"
  with `max_tokens=5`, and prints one line: `llm-check ok: model=... input_tokens=... output_tokens=...
  [cache_hit_tokens=...] latency_ms=...`. It never prints the reply. On an `LLMError` it prints
  `llm-check failed: <class>: <message>` to stderr and exits 1; an invalid configuration still exits 2.
  The request lives in `services/llm_check.py` (`check_llm`), the printing and exit codes in `main.py`.
- **Not verified against the live API.** No key was available. The request and response shapes follow
  the DeepSeek documentation read at the time of CYR-13; see [decisions.md](decisions.md) for exactly
  what was and was not confirmed.

## Persona and style examples (CYR-14)

Data and rules for the user: [persona-and-examples.md](persona-and-examples.md). This section is the
code map. Nothing here is wired into `main.run`, calls an LLM or sends a message; the prompt
builder (CYR-16, see [Reply generation](#reply-generation-cyr-16)) consumes `PersonaService` and
`ExampleSelector`.

| Where | What |
| --- | --- |
| `domain/style.py` | Limits as named constants, `Turn`, `PersonaCreate`, `Persona`, `StyleExampleCreate`, `StyleExample`, `ExamplesAddResult`, `content_hash`. Texts are cleaned (NUL removed, trimmed) by the types; every model hides input values in its validation errors |
| `domain/errors.py` | `ContentProblem` (a location and a rule), `ContentValidationError` (prints up to 20 problems), `ImportFileError` |
| `services/style_files.py` | `read_import_file`, `parse_persona`, `parse_examples`: the file formats and their validation, pure apart from reading. Pydantic errors become `ContentProblem`s from the error location and type only |
| `services/style_import.py` | `PersonaImporter` and `ExamplesImporter`: read, parse, store, return counts (`PersonaImportResult`, `ExamplesImportResult`) |
| `services/persona_service.py` | `PersonaService.get()` |
| `services/style_examples.py` | `ExampleSelector.select(mode_name, limit)` and `collect_example_stats` |
| `services/repositories.py` | `PersonaRepository` (`get`, `replace`) and `StyleExampleRepository` (`add_many`, `list_all`, `count`, `delete_all`) |
| `db/models.py`, `db/repositories.py` | `PersonaRow`, `StyleExampleRow`, `SqlPersonaRepository`, `SqlStyleExampleRepository`; migration `0002` |
| `style_cli.py`, `main.py` | The commands `persona import FILE`, `persona show`, `examples import FILE [--replace --yes]`, `examples stats` and the Makefile targets `persona-import`, `examples-import`, `examples-stats` |

- **Stored, not read from a path.** The containers cannot see the owner's files, so the commands
  run locally against the host database port (like `make migrate`, with the same `Settings`) and
  the runtime reads PostgreSQL.
- **Validation first.** The whole examples file is validated before anything is written. Failures
  report locations and rules, never texts. The import is one transaction; `add_many` with
  `replace=True` deletes and inserts in that same transaction.
- **Selection** reads all examples (`list_all`, newest first) and filters in Python. That is fine
  for the hundreds of dialogues this product holds; a SQL filter is a later optimization if it
  ever matters.
- **Logging.** Logger `cyrano.services.style_import`: INFO with counts only. A test imports the
  sample files with all loggers at DEBUG and asserts that no persona or example text appears in
  the logs or in the errors.
- **Exit codes.** The commands exit with 0, or with 1 and a readable message on any error (a
  storage failure prints the exception class only). An invalid configuration exits with 2 like
  every command.

## Chat export converter (CYR-34)

Data and rules for the user: [persona-and-examples.md](persona-and-examples.md#from-a-telegram-desktop-export).
`python -m cyrano examples convert INPUT (--me NAME | --me-id ID) ...` turns a chat exported by
Telegram Desktop (machine-readable JSON, one chat) into a draft examples file. It reads a file and
writes a file: no database, no network, no Telegram, no LLM. It **needs no `Settings`**:
`main.main` parses the arguments and dispatches it before `load_settings`, so it works without
`.env` and credentials. (The other `examples` and `persona` commands still need the full
`Settings`; that is unchanged.)

| Where | What |
| --- | --- |
| `domain/chat_export.py` | Constants (size cap, defaults, ranges), `SkipReason`, `RedactionKind` and its token, `ExportMessage`, `ChatTurn`, `ConvertOptions`, `ConvertReport` |
| `services/chat_export_reader.py` | `load_export_file` (the file checks), `message_items`, `read_messages`: tolerant reading of the JSON into `ExportMessage`s; every message goes through a Pydantic model with `extra="ignore"` and `JsonValue` fields, so an unknown or missing field never crashes it |
| `services/chat_redaction.py` | `Redactor`: one compiled pattern, one pass, tokens `<email>`, `<phone>`, `<link>`, `<username>`, `<name>` |
| `services/chat_dialogues.py` | Pure: `split_conversations`, `build_turns`, `truncate_turns`, `dialogue_candidates`, `drop_duplicates`, `sample_evenly` |
| `services/chat_export_converter.py` | `validate_options`, `build_draft` (the pipeline), `render_draft`, the output guards, `write_draft` (atomic, validated) and `convert_chat_export` |
| `convert_cli.py` | Arguments, the request, the counts-only report |

- **Pipeline.** Read and order the messages, check the owner is among the senders, redact every
  message (a message that becomes only tokens is dropped), split into conversations, merge into
  turns, truncate long turns, build one candidate per owner turn, drop exact duplicates, sample.
  Redaction comes first so no later stage handles raw sensitive text.
- **Owner.** `--me` is compared with the `from` name and `--me-id` with `from_id`, both exactly
  and case-sensitively; exactly one is required. In id mode the name may be null or odd.
- **Output.** The draft is written only under `data/` of the current directory (symlinks and `..`
  resolved) unless `--allow-any-path`; an existing file needs `--force`; the text goes to a
  private temporary file in the same folder (mode 0600), is validated by the real CYR-14 validators
  (`read_import_file`, `parse_examples`) and only then renamed over the target. A failure removes
  the temporary file.
- **Logging.** Logger `cyrano.services.chat_export_converter`, one INFO line with counts. Stdout
  and stderr carry counts, option names and paths, never a text, a name, an id or a number from the
  chat. No logging is configured by the command (it has no `Settings`), so INFO is not shown.
- **Tests.** `tests/services/test_chat_export_*.py`, `test_chat_dialogues.py`,
  `test_chat_redaction.py` and `tests/cli/test_convert_cli.py`: all fixtures are invented and built
  in the tests; one test runs the command in a subprocess with an empty environment; one imports the
  written draft through `ExamplesImporter` into the test database; one asserts that the converter
  modules import neither `config`, `db`, Telethon, aiogram nor httpx.

## Reply generation (CYR-16)

Rules and shapes for the reader: [reply-generation.md](reply-generation.md). This section is the
code map and the decisions about layering. Nothing here is wired into `main.run`, writes to the
database or sends a message.

| Where | What |
| --- | --- |
| `prompts/texts.py`, `prompts/render.py` | Every fixed text and the pure rendering of the system and the user message |
| `domain/reply.py`, `domain/errors.py` | The models, the limits (`MIN_HISTORY_MESSAGES` 4, `REPLY_MAX_CHARS` 600, the four fact limits) and `PersonaMissing`, `ModeNameNotFound`, `NoMessagesToReplyTo`, `PromptTooLarge`, `ReplyInvalid` |
| `services/contact_facts.py` | `build_contact_facts(contact)`: name, age, city, about, bio, truncated; the only function that decides which profile fields reach a prompt |
| `services/prompt_builder.py` | `PromptBuilder(budget_chars, timezone, clock)`: a synchronous `build(...)` that renders the two messages and trims examples, then the oldest messages, to the character budget |
| `services/reply_postprocess.py` | `clean_reply`, `cap_length`: pure |
| `services/reply_generator.py` | `ReplyGenerator`: `generate`, and `prepare` plus `complete`, its two halves |
| `reply_cli.py`, `main.py` | The `reply-preview` command and its wiring |

- **Dependencies.** `ReplyGenerator` depends on the `ContactRepository`, `MessageRepository` and
  `ModeRepository` Protocols, `PersonaService`, `ExampleSelector`, `PromptBuilder` and the
  `LLMClient` Protocol. It knows nothing about SQLAlchemy, httpx or the provider; `main.py` passes
  in the client bound to the `reply` step.
- **Where the builder lives.** The text is in `prompts` (a leaf, easy to review and to change);
  the assembly and the budget logic are in `services`, because they decide what data reaches the
  provider and need the domain models. `llm` stays ignorant of prompts.
- **Clock and zone.** The builder takes a clock (`Callable[[], datetime]`) and a `ZoneInfo`
  (`Settings.tzinfo`), so tests fix both.
- **Tests.** `tests/prompts/`, `tests/services/test_prompt_builder.py`,
  `test_reply_postprocess.py`, `test_contact_facts.py`, `test_prompt_snapshot.py` (one full prompt
  from invented data against an expected text), `test_reply_generator.py` (real PostgreSQL, a
  scripted fake client), `tests/cli/test_reply_cli.py`. All data is invented; the privacy tests use
  the 12-digit markers of `tests/privacy.py`.

## Persistence

- **Tables**: `contacts`, `messages`, `modes`; native enums `contact_status` and
  `message_direction`. Columns and rules: [contact-lifecycle.md](contact-lifecycle.md). Since
  CYR-14 also `persona` (one row, `id` fixed to 1 by a check constraint) and `style_examples`
  (JSONB `tags` and `turns`, unique `content_hash`); see
  [persona-and-examples.md](persona-and-examples.md).
- **Names**: table names and enum type names live in `db/models.py`, enum values in
  `domain/enums.py`. `Base.metadata` has a naming convention, so constraint and index names are
  stable and need no spelling out. The migration repeats these strings on purpose: a revision is a
  frozen record of the past, so it must not change when the code does. `alembic check` and
  `tests/db/test_migrations.py` prove the two agree.
- **Time**: every timestamp is `timestamptz`; the domain models reject naive values and normalize
  to UTC. `touch_last_message_at` refuses a naive `datetime`.
- **Repositories**: take a session factory. Every method is one transaction (`StyleExampleRepository.add_many(..., replace=True)` deletes and inserts in that one transaction, which is how the import's `--replace` stays atomic), returns domain models
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
| `DEEPSEEK_MODEL` | no | `deepseek-chat` | model of every step that has no model of its own; must not be blank |
| `DEEPSEEK_BASE_URL` | no | `https://api.deepseek.com` | `http://` or `https://` URL; not in the startup log line |
| `DEEPSEEK_THINKING` | no | `false` | `true` sends `thinking: {"type": "enabled"}`; by default thinking is sent as disabled |
| `LLM_REPLY_MODEL`, `LLM_SUMMARY_MODEL` | no | `DEEPSEEK_MODEL` | model of the reply and of the summary step; must not be blank |
| `LLM_REPLY_TEMPERATURE`, `LLM_SUMMARY_TEMPERATURE` | no | `1.0`, `0.2` | 0 to 2 |
| `LLM_REPLY_MAX_TOKENS`, `LLM_SUMMARY_MAX_TOKENS` | no | `400`, `1500` | positive integers |
| `TELETHON_LOG_LEVEL` | no | `WARNING` | |
| `AIOGRAM_LOG_LEVEL` | no | `WARNING` | |
| `HTTPX_LOG_LEVEL` | no | `WARNING` | also applied to `httpcore` |
| `LEOMATCH_BOT_USERNAME` | no | `leomatchbot` | username of the matching bot whose chat is searched for a contact's texts; a leading `@` is removed; the default is an assumption to check in Telegram; not in the startup log line |
| `STYLE_EXAMPLES_MAX` | no | `8` | most style examples one selection returns; 1 to 50; not in the startup log line |
| `REPLY_HISTORY_MESSAGES` | no | `20` | how many last messages of a contact the reply prompt loads; 1 to 100; not in the startup log line |
| `REPLY_PROMPT_BUDGET_CHARS` | no | `24000` | character budget of the whole reply prompt; 4000 to 200000; not in the startup log line |
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
  `cyrano.services.contact_events`, the collector `cyrano.services.profile_collector`, the announcer
  `cyrano.services.card_announcer`, the control bot `cyrano.bot` with its submodules, the LLM
  client `cyrano.llm.deepseek` and `cyrano.llm.transport`, and the persona and example import
  `cyrano.services.style_import` (INFO: counts only), and the reply generator
`cyrano.services.reply_generator` (INFO: counters and token counts only, never a prompt or a reply).
- Library loggers (`telethon`, `aiogram`, `httpx`, `httpcore`) have their own levels
  (`*_LOG_LEVEL`, default `WARNING`), independent of `LOG_LEVEL`. `httpcore` follows
  `HTTPX_LOG_LEVEL` because it is httpx's transport.
- A `SecretStr` passed in `extra=` is masked as a second line of defence.
- **Startup line**: the app and the worker log `Settings.safe_summary()`, which returns only the
  settings listed in `LOGGED_SETTINGS` (log levels, timezone, media dir, model name, heartbeat
  path, the LLM models, temperatures and max tokens, the thinking flag; not the base URL). No secret, no `OWNER_TELEGRAM_ID`, no `TELEGRAM_API_ID`, no session path. A new setting is
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
  CYR-12 added: card rendering (escaping, truncation, keyboard, callback size), the card sender
  with a recording `BaseSession` that returns real aiogram objects (photos, retry, forbidden,
  chat not found, every call addressed to the owner), the handlers driven through
  `Dispatcher.feed_raw_update` (owner-only, `/start`, every callback outcome), `ControlBot` polling
  against the same recording session, the card builder, the announcer with in-memory and with real
  repositories, `CardActions` on real PostgreSQL (including two simultaneous presses), the import
  direction for aiogram, and in `tests/test_main.py` the start order, the shutdown order, a failing
  poll and a crashing announcer. `tests/test_main.py` and `tests/fake_app.py` replace `ControlBot`
  with `FakeControlBot`, so no test builds a real bot.
  CYR-33 added: the lookup over albums (caption on the first, a middle and the last message, an
  album of one, a plain card, a video-only card, an album as one position in both chain shapes, a
  captionless album, the guard inside a position, the limits of 2 positions and 5 photos), the
  numbered card photo downloads with failures that never shrink the stored list, the legacy profile
  shape, the fallback name, and the media group of the control bot. The unreadable-file test fakes
  the access check and the open error instead of `chmod`, so it behaves the same as root.
  CYR-13 added: the LLM client against `httpx.MockTransport` (no network, no `respx`): the request
  body, the reply and its usage, the empty and truncated replies, `complete_json` (bare and fenced
  JSON, the schema message position, the correction turn, an empty reply, exhausted attempts with
  the raw reply absent from errors and logs), the transport (retry then success per error kind,
  exhaustion, `Retry-After` in seconds and as a date, beyond the cap, no retry on 4xx, a hanging
  attempt against the deadline, no httpx exception leaking, the backoff values through a fake
  sleep), the per-step settings and their validation, the logs, `llm-check` with a fake client and
  with a mocked transport, and the import direction for httpx and the provider. `FakeLLMClient` in
  `tests/services/fakes.py` is the fake for the consumers (CYR-16, CYR-17, CYR-25);
  `ScriptedLLMClient` there replays a list of replies and errors.
  CYR-14 added: the domain models (limits at, under and over the boundary, tag normalization, the
  content hash, validation errors without input values), the file validation (every rule violated
  once, 20 problems at most printed, invalid JSON, a missing, oversized, non-UTF-8 file and a
  directory, no text in any message), the repositories on real PostgreSQL (counts, idempotency, the
  single persona row, a replace that fails and leaves the old rows intact), the importers, the
  selector (mode before generic, newest first, other modes never leak, case-insensitive tags,
  limits, deterministic ties), the commands with the real repositories and through `main`, migration
  `0002` (upgrade, downgrade to `0001`, no drift), the new setting, the sample files against the real
  validators with the `.gitignore` and `.dockerignore` content, and a captured full import that
  must not contain any sample text.
  CYR-16 added: the layered prompt (order and headers, every optional layer omitted when absent, the
  facts allowlist and truncation, the summary slot with a fake text, the dialogue block with roles,
  order, multi-line messages, the boundary token removed even when rebuilt from pieces, an injection
  attempt staying inside the block, the date line from an injected clock and zone), the budget
  (examples dropped first from the end, then the oldest messages, never below the minimum, counters,
  the persona untouched, `PromptTooLarge`, no incoming message, a last outgoing message), the reply
  cleaning (every rule and combinations, the cap at a sentence boundary, the refusals), the
  generator on real PostgreSQL with a scripted fake client (the exact two messages, mode fallback,
  every error, no database writes, captured logs without any invented text or marker), one prompt
  snapshot, the `reply-preview` command (format, `--show-prompt`, each error and exit code, no row
  changes, through `main` with a mocked provider), the new settings and the import direction for
  `prompts`.
- Failure cases to cover for new behaviour:
  empty input, malformed Telegram event, invalid LLM JSON, status changed between scheduling and
  sending, FloodWait.

## Open design questions

Listed in [decisions.md](decisions.md) under "Open questions": how the worker sends through the
userbot session, worker reconnect to Redis, tuning the reply instructions on real outputs, whether
`bot`, `userbot` and `worker` may import each other, and the Leomatch cases not yet observed (a card with a description, search, the caption position and message order inside an album, a video card).
