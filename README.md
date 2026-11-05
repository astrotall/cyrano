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
- **The owner can always take over.** If you write to a contact by hand from the second account,
  automatic replies to that contact stop at once (see [When you write by hand](#when-you-write-by-hand)).
- **Pacing and limits are central.** The read mark, typing, quiet hours, rate limits and FloodWait
  handling live in the sender, not in callers. The delayed send time comes from one planner. Message
  splitting will join them (CYR-36).
- **Stop-triggers hand over.** Meeting or date proposals, phone and contact exchange, money,
  photo requests, a direct question whether it is a bot or AI, and low model confidence stop the
  conversation and notify the owner. The assistant does not answer those itself.
- **Style comes from a persona and examples, not training.** A common persona text, a prompt
  fragment per mode and few-shot examples from the owner's real chats. No model is trained.
- **Personal data stays local.** Persona, examples, sessions, media and messages never go to git.
  Message text, names and other personal data are never logged at INFO or above.

> The project skeleton (CYR-8), the storage layer (CYR-9), the userbot intake (CYR-10), the
> profile collection (CYR-11), the control bot with the contact card (CYR-12), the LLM client
> (CYR-13), the import of the owner persona and style examples (CYR-14), the converter of a chat
> export into draft examples (CYR-34) and the layered reply prompt with the reply generator (CYR-16)
> exist; these last four are not used by the running app, only by the `llm-check`, `persona`,
> `examples` and `reply-preview` commands. The outbox with the one guarded sender (CYR-19) and its
> limits, pause and stop (CYR-22) exist too, and only the owner's test command can add a message to it: nothing is sent automatically. The pause
> on a message the owner writes by hand (CYR-21) exists. The flow below is the plan.

## Architecture diagram

Planned flow. The boxes marked (planned) do not exist yet. The userbot box exists for listening and
registering contacts only; the control bot box exists for the card and its two buttons.

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
 | owner only (exists)    |                                        |
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
 | sender: outbox, status guard, rate limits,    |
 | read mark, typing, quiet hours (exist);       |  --> Telegram
 | splitting (planned)                           |
 +-----------------------------------------------+
```

What exists: configuration, JSON logging, the app process with a heartbeat, the Arq worker with a
placeholder job, the contact status rules, the database schema with its migrations and
repositories, Docker and CI, and the userbot: it logs the second account in, listens for private
messages, registers unknown senders as contacts with status `new`, stores their messages and
publishes an in-memory event for each new contact; a collector then reads their profile for the
owner's card (see [What is collected](#what-is-collected-about-a-new-contact)). The control bot then shows the owner a card for every new contact with a Reply button per
mode and an Ignore button (see [The control bot](#the-control-bot-and-the-card)). The outbox and
the one sender send what the owner's test command puts in the outbox, and nothing else (see
[The outbox and the sender](#the-outbox-and-the-sender)). The `llm`
package holds the DeepSeek client, which nothing in the app calls yet; `services` holds the
repository interfaces, the LLM interface, the intake, the event queue, the collector and the card
logic.
Details: [.claude/docs/contact-lifecycle.md](.claude/docs/contact-lifecycle.md).

### What the userbot does and does not do

Does:

- listens on the second account and handles only real private messages from real users;
- registers an unknown sender as a contact with status `new` and stores the message (a photo or
  sticker counts too: the text is the caption or empty, the media is not downloaded);
- processes messages that arrived while the app was down (catch-up on start); a redelivered message
  is stored once.
- reads the profile of a new contact (username, name, bio, photo, the matching-bot text) for the
  owner's card, read-only and one contact at a time;
- watches the messages you send by hand to a contact from this account, to pause automatic replies
  to that contact (CYR-21). It reads them; it never answers them.

Does not:

- send anything by itself, mark messages as read or answer anyone, unknown senders included. The
  one way out is the guarded sender, and only the owner's test command gives it something to send;
- react to groups, channels, bots, deleted accounts or Telegram's service account; a message it sent
  itself through the sender is recognised and ignored;
- change a status. The step from `new` to `pending` happens in the card announcer, after the card
  is delivered to the owner.

### What is collected about a new contact

After a contact is registered, the profile collector gathers what the owner needs to decide from
one card: the username, the display name, the bio, the current
profile photo and, if there is one, the contact's profile card from the matching bot (its text and photos). It reads
only; it never sends anything to the contact or to the matching bot, and nothing is marked as
read. A failed part never blocks the others or the registration.

- **Where it is stored.** Texts go to the `profile` column of the contact (a JSON object, see
  [contact-lifecycle.md](.claude/docs/contact-lifecycle.md#the-stored-profile)). The photo is a file
  `profiles/<internal contact id>.jpg` under `MEDIA_DIR` (inside the container `/data/media`, on
  the `media` volume, or in `./data/media` with the dev override); the profile keeps its path relative to `MEDIA_DIR`. Both are personal data:
  they are never logged and `media/` and `data/` are git-ignored.
- **The matching-bot lookup.** The collector looks in the owner's chat with the matching bot
  (`LEOMATCH_BOT_USERNAME`) for the message that announces the match with this person: it links to
  the person by Telegram id, or by `t.me/<username>` or `@username`, compared exactly (`ann` never
  matches `joanna` or `ann_k`). It tries Telegram's search for the username first and falls back
  to the last 100 messages. The matching bot sends a card as one message group (up to 3 photos with
  the caption on one of them, or one video with a caption), and that group counts as one position:
  the card sits at most two positions before the match message. The card text and the match text
  are stored apart, and the match text is never shown as the profile; name, age and city are
  extracted only from a card that looks like `Name, age[, city]`, optionally followed by
  ` - description`, after an optional header line that ends with a colon. When the card has no
  usable name, the name is taken from the link in the match message. All photos of the card (at
  most five; a video is never downloaded) are saved as
  `profiles/<internal contact id>-card-<n>.jpg` under `MEDIA_DIR`, with their relative paths stored
  next to the card text, and a failed download never blocks the rest.

### The control bot and the card

The control bot lives on the owner's main account and answers only `OWNER_TELEGRAM_ID`; an update
from anyone else is dropped without a reply.

**Starting it.** Create the bot with @BotFather and put its token in `BOT_TOKEN`. Then open the bot
in Telegram **from the owner account and send `/start` once**. A bot cannot write to a person who has
never started it, so without this step cards cannot be delivered; the contact stays `new` and the
announcer retries every two minutes until the owner has pressed Start. The bot starts together
with the app, after the userbot is connected. A rejected token ends the app with a readable error.

**What the owner sees.** For every new contact, after its profile was collected (or after ten
minutes at most, so a failing collection never keeps the owner waiting):

- the photos first, as one media group: the profile photo and then all card photos, with a caption
  on the first one; a missing photo file is skipped;
- then one message with the name, the nick, the first messages the contact wrote (up to three), the
  bio and the matching-bot card (a short line when the card was not found), plus a line when the
  profile could not be collected. Names, bio
  and message texts come from strangers and are shown as plain text, never as markup;
- under that message the buttons: one **Отвечать: <mode>** per enabled mode and **Игнорировать**.

Reply moves the contact from `pending` to `active` with that mode, Ignore from `pending` to
`ignored`; the card is then edited to show the decision and the buttons disappear. A button on an
old card never changes a contact that is no longer waiting for a decision: it shows a short alert
instead. Nothing replies to an `active` contact automatically yet (the autopilot is CYR-17).

### When you write by hand

If you write to a contact yourself from the second account, the assistant must not talk over you.
When a contact in status `active` gets a message from you:

- the contact becomes `paused` (the reason is stored as `manual_message`) and its planned messages
  are cancelled, so nothing is sent to it automatically from then on;
- you get **one** message in the control bot: «Вы написали контакту #<id> сами: автоответы для него приостановлены, запланированные
  ответы отменены…», with the button **Возобновить автоответы**. It names the contact by the internal
  number only, never by name or text;
- your message is stored in the history like the others. It does not count as a message from the
  contact.

To go back to automatic replies press the button. The contact becomes `active` again with the mode it
had, and the notice is edited to say so. A second press does nothing. Until you press it, nothing is
sent to that contact. Writing again to a paused contact changes nothing and sends no second notice.

Good to know:

- a message sent from another device logged in to the same account looks the same and pauses too;
- if the assistant was just sending a message while you wrote, it waits up to about 16 seconds to be
  sure which of the two it saw, and a message that was already on its way is stopped before it is sent;
- only a sent message counts. Reading the chat or typing does not pause anything;
- a contact that is not `active` (new, waiting for your decision, ignored or already paused) is
  not changed by your messages and you get no notice.

Details: [.claude/docs/contact-lifecycle.md](.claude/docs/contact-lifecycle.md#manual-intervention-cyr-21).

### The outbox and the sender

Every message to a contact goes through one table and one sender. A message is a row in the outbox
(`scheduled_messages`). The sender, a background task of the app, takes one due row at a time,
re-reads the contact, checks that it is still `active`, and only then sends. A row that may or may
not have reached Telegram is never sent again: it is marked failed and the owner is told, because a
duplicate message to a person cannot be taken back. Only a flood wait, a reached send limit and a
closed window put a row back automatically; the sender calls `send_text` once for a row whatever else
goes wrong. The sender
also keeps conservative limits per minute, hour and day, pauses as a whole during a flood wait, and
stops completely on a problem with the account itself. Notifications to the owner carry
internal ids and error class names only.
The autopilot does not exist yet, so the owner's commands are the only source of rows. States,
errors and rules: [.claude/docs/outbox-and-sender.md](.claude/docs/outbox-and-sender.md).

```sh
make outbox-enqueue CONTACT=<contact id> TEXT="..." [IN=<seconds>]   # default 5 seconds
make outbox-list [STATE=<state>] [LIMIT=<n>]                         # ids, states, send_at, due_at, error, cancel_reason; never a text
make outbox-cancel ID=<row id>                                       # scheduled rows only
make outbox-retry ID=<row id> [IN=<seconds>] YES=1                   # failed rows only; check the chat first
make outbox-status                                                   # sender state, counters against limits, rows per state
make outbox-resume [YES=1]                                           # clears a pause; a stop needs YES=1
```

**After a notification that the outcome is unknown.** The send failed in a way that does not tell
whether the message reached Telegram (a timeout or a lost connection, for example), so the sender did
not repeat it. Open the chat on the second account. If the message is there, do nothing: the row
stays `failed`. If it is not, run `make outbox-retry ID=<row id> YES=1` (without `YES=1` the command
only prints a reminder). The row goes out once, after the usual status check, so a contact that is
no longer `active` gets nothing.

The commands run on the host like `make migrate`: they need a complete `.env` and the database up. A
refusal or any other error prints one line and exits with 1 (the `make` target reports that as its
own failure). The contact must be `active`; `AUTOPILOT_ENABLED` does not apply to these commands.

**After a pause notice.** Telegram asked to wait, so nothing goes out to anyone until that time, and
sending then goes on by itself, also after a restart. Usually do nothing; `make outbox-status` shows
the time. `make outbox-resume` ends the pause early, at the risk of another flood wait.

**After a stop notice.** Telegram refused because of the account itself (for example `PeerFloodError`,
a lost session or a frozen account). Nothing goes out until you act, also after a restart. While
the sender stays stopped, a short reminder comes every 6 hours. Check the account in
Telegram; if the session is gone, run `make login` and restart the app. Then run
`make outbox-resume YES=1`. Without `YES=1` the command only prints a reminder and exits with 1.
Rows more than 6 hours past their original due time when sending resumes are cancelled as `stale`,
not sent late; `make outbox-list` shows the reason, and one notice tells you how many were cancelled.

**After a limit notice.** An hourly or daily limit is reached, and rows are postponed until a slot
frees up. A row that ends up more than 6 hours past its original due time is cancelled as `stale`
instead of going out late, and you get one summary notice with the count (at most once an hour). Usually do nothing; `make outbox-status` shows the counters. The limits are the
`SEND_LIMIT_*` settings.

**After a notice that a contact was paused.** Telegram refused the message because of this contact
(blocked, privacy settings, deactivated). The contact is `paused`, and its other planned rows are
cancelled. The notice has no Resume button; a general Resume is CYR-26. A text that is too long fails only its own row.

**Verifying the sender live.** With the app running and a contact you control approved through its
card, run `make outbox-enqueue CONTACT=<id> TEXT="test" IN=10`, then `make outbox-list`: the row moves
from `scheduled` to `sent`, and the message appears in that chat on the second account. To see the
guard, enqueue for a contact that is not `active`: the command refuses with `contact_not_active` and
stores nothing. To stop a planned row, enqueue with a longer `IN` and use `make outbox-cancel`. This
uses a real account, so keep it to a few messages to a contact of your own.

### Humanization and the schedule

Replies should look like they come from a person with a daily routine. A planner decides when a reply
goes out (a short delay inside a conversation, a longer one otherwise, past the night and the busy
windows), and the sender marks the chat as read, pauses and shows typing before every message. A failed
read mark or typing never blocks the message. An autopilot row due inside a window is postponed, not
dropped; the owner's test rows skip that check but get the read mark and typing too. The autopilot does
not exist yet, so nothing calls the planner except `schedule plan`. Splitting a reply is CYR-36.

Without an imported file the built-in default profile is used: night from 01:00-02:00 until 09:00, on
Saturday and Sunday until 11:00-12:00, no busy windows, in `TIMEZONE`.

**Your own routine.** The real file lives only at `data/schedule.json`, which never goes to git:

```bash
mkdir -p data
cp examples/schedule.sample.json data/schedule.json   # then put your own times in it
make schedule-import FILE=data/schedule.json          # validates the whole file; a bad file stores nothing
make schedule-show                                    # the profile in use
make schedule-check AT="2026-11-03 09:30"             # free, or blocked until a time, and the kind
make schedule-plan CONTACT=<id>                       # when a reply to their last message would go out
```

The running app picks up an import without a restart. Format, rules and worked examples:
[.claude/docs/humanization.md](.claude/docs/humanization.md).

### Code layers

Import direction (enforced by review, and by a test for `domain`, `prompts`, `db`, `services`, `userbot` and
`bot`; `userbot` is the only package that may import Telethon and `bot` the only one that may
import aiogram; full rules in
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
| Userbot | Telethon | now (listens, registers contacts, reads profiles; sends only through the guarded sender) |
| Control bot | aiogram 3 | now (owner-only, contact card with Reply and Ignore) |
| ORM and migrations | SQLAlchemy 2.x (async), asyncpg, Alembic | now (schema and repositories, used by the intake) |
| LLM client | DeepSeek over plain httpx, no SDK | now (client, retries, `make llm-check`; not wired into the app yet) |
| Persona and style examples | Imported from local files, stored in PostgreSQL | now (import commands and selectors; used by the reply preview only) |
| Reply prompt | Layered prompt builder, reply cleaning, `reply-preview` command | now (nothing in the app generates a reply by itself yet) |
| Lint, format | Ruff | now |
| Types | mypy strict with the Pydantic plugin | now |
| Tests | pytest, pytest-asyncio | now |
| HTTP mocks in tests | `httpx.MockTransport` (no respx) | now |
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
├── Makefile                      # install, check, test, fmt, up, down, logs, migrate, revision, db-up, login, login-docker, llm-check, persona-import, examples-import, examples-stats, examples-convert, reply-preview, outbox-enqueue, outbox-list, outbox-cancel, outbox-retry, outbox-status, outbox-resume
├── Dockerfile                    # multi-stage, non-root runtime
├── docker-compose.yml            # app, worker, one-shot migrate, postgres, redis
├── docker-compose.override.yml   # dev: Postgres and Redis on 127.0.0.1, media in ./data/media
├── .env.example                  # bare template, no comments
├── alembic.ini                   # points at migrations/; no URL, it comes from Settings
├── migrations/
│   ├── env.py                    # async environment, URL from Settings
│   └── versions/                 # 0001 creates the tables and seeds the default mode; 0002 adds persona and style_examples; 0003 adds scheduled_messages; 0004 adds sender_state; 0005 adds due_at; 0006 adds schedule_profile; 0007 adds contacts.pause_reason
├── examples/                     # invented sample persona, style examples and schedule (the real files go to data/, never to git)
├── .dockerignore
├── .gitignore
├── .claude/docs/
│   ├── architecture.md           # layers, import rules, processes, config, logging
│   ├── contact-lifecycle.md      # statuses, transitions, message flow
│   ├── persona-and-examples.md   # persona and style examples: formats, limits, commands, privacy
│   ├── humanization.md           # schedule profile, planner rules, pre-send sequence, commands
│   ├── reply-generation.md       # the layered prompt, the budget, the reply cleaning, reply-preview
│   ├── outbox-and-sender.md      # the outbox, the one sender, the guard, limits, pause and stop, errors, owner commands
│   ├── safety-rules.md           # S1..S10
│   └── decisions.md              # decided and open
├── .github/
│   ├── dependabot.yml            # weekly: Actions, Docker, Compose, uv
│   └── workflows/ci.yml
├── src/cyrano/
│   ├── __main__.py               # python -m cyrano [login | llm-check | persona ... | examples ... | reply-preview ... | outbox ... | schedule ...]
│   ├── main.py                   # app entrypoint, signals, heartbeat, subcommands login, llm-check, persona (import, show), examples (import, stats, convert), reply-preview, outbox (enqueue, list, cancel, retry, status, resume), schedule (import, show, check, plan)
│   ├── style_cli.py              # the persona and examples commands
│   ├── convert_cli.py            # examples convert: Telegram Desktop export to a draft examples file
│   ├── reply_cli.py              # reply-preview: generate one reply for a stored conversation
│   ├── outbox_cli.py             # outbox enqueue, list, cancel, retry, status, resume: the owner's commands
│   ├── schedule_cli.py           # schedule import, show, check, plan: the owner's commands
│   ├── config.py                 # Settings, load_settings, ConfigError
│   ├── logging.py                # JSON-lines logging
│   ├── health.py                 # heartbeat file and the app healthcheck
│   ├── userbot/                  # Telethon: protocols, filters, handler, client, adapter (the only send), login, runner, profile and Leomatch readers
│   ├── bot/                      # aiogram: owner-only middleware, handlers, card rendering, card sender, owner notifier, polling
│   ├── llm/                      # DeepSeek client, retrying transport, factory (the only package that imports httpx)
│   ├── prompts/                  # prompt texts and their pure rendering (imports only domain)
│   ├── domain/                   # enums, status transition rules, errors, Pydantic models
│   ├── db/                       # SQLAlchemy models, engine and sessions, repositories
│   ├── services/
│   │   ├── repositories.py       # repository Protocols
│   │   ├── llm_client.py         # LLMClient Protocol
│   │   ├── llm_check.py          # the request behind llm-check
│   │   ├── intake.py             # IntakeService: register contact and message
│   │   ├── sanitize.py           # NUL-byte removal, text cleaning
│   │   ├── contact_events.py     # generic ContactEventPublisher, bounded in-memory queue
│   │   ├── profile_source.py     # ProfileSource, LeomatchSource Protocols, ProfileSourceError
│   │   ├── leomatch.py           # parser of the Leomatch card text
│   │   ├── profile_collector.py  # ProfileCollector: profile, photo, Leomatch text per new contact
│   │   ├── style_files.py        # reading and validating the persona and examples files
│   │   ├── style_import.py       # PersonaImporter, ExamplesImporter
│   │   ├── persona_service.py    # PersonaService
│   │   ├── style_examples.py     # ExampleSelector, example statistics
│   │   ├── chat_export_reader.py # reads a Telegram Desktop JSON export into plain messages
│   │   ├── chat_redaction.py     # replaces e-mails, phones, links, usernames and names with tokens
│   │   ├── chat_dialogues.py     # conversations, turns, dialogue candidates, sampling
│   │   ├── chat_export_converter.py # the conversion and the guarded, atomic write of the draft
│   │   ├── contact_facts.py      # the profile facts that may reach a prompt
│   │   ├── prompt_builder.py     # PromptBuilder: the layered prompt and the budget trimming
│   │   ├── reply_postprocess.py  # cleaning of the model reply
│   │   ├── reply_generator.py    # ReplyGenerator: build, call the LLM, clean
│   │   ├── card_builder.py       # the owner's card from the stored contact
│   │   ├── card_sender.py        # CardSender Protocol
│   │   ├── card_announcer.py     # CardAnnouncer: card to the owner, new -> pending
│   │   ├── card_actions.py       # CardActions: Reply (pending -> active) and Ignore
│   │   ├── send_guard.py         # the status and source check, at enqueue and before sending
│   │   ├── outbox_service.py     # OutboxService: text validation, guard, row
│   │   ├── send_limiter.py       # the six send limits, counted from stored messages
│   │   ├── outbox_sender.py      # OutboxSender: the only caller of the transport
│   │   ├── humanization.py       # the send time planner, ScheduleService, HumanizationPlanner
│   │   ├── schedule_files.py     # validating and importing the schedule file
│   │   ├── message_transport.py  # MessageTransport Protocol: send_text, show_typing, mark_read
│   │   ├── owner_notifier.py     # OwnerNotifier Protocol: notify
│   │   ├── manual_intervention.py # ManualMessageDetector: a manual message pauses an active contact
│   │   ├── manual_pause_notifier.py # ManualPauseNotifier Protocol: notify_manual_pause
│   │   └── pause_actions.py      # PauseActions: Resume of a paused contact
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
  `media` (app and worker; the dev override replaces it with the `./data/media` bind mount, see
  below). `down` keeps them.
- **Logs.** `make logs` shows JSON lines; how to read the sender's read mark, pause, typing and send
  steps from them is in [Reading the logs](.claude/docs/outbox-and-sender.md#reading-the-logs).

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

The override also bind-mounts `./data/media` over `/data/media` in `app` and `worker`, so collected
photos appear in `./data/media` inside the project folder and can be opened from the host; the
named `media` volume is not used with the override. Without it (production) nothing changes: the
named volume stays.

- Run `mkdir -p data/media` before the first start. Otherwise Docker may create the folder owned
  by root on Linux.
- On a Linux host the container user (uid 10001, `cyrano`) must be able to write to the folder,
  for example `sudo chown 10001:10001 data/media` or `chmod 777 data/media`.
- On Docker Desktop for Mac and Windows file ownership is mapped automatically; nothing to do.

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
The app connects to Telegram as the userbot and polls the control bot, and connects to Postgres
(from the start: the sender polls the outbox every two seconds); it does not connect to Redis. The worker connects to Redis; the compose one works via `docker compose up -d redis`.

### Makefile targets

| Target | What it does |
| --- | --- |
| `make install` | `uv sync --locked` |
| `make check` | Ruff lint, Ruff format check, mypy strict |
| `make test` | pytest; database tests need `make db-up` |
| `make fmt` | Ruff fix and format |
| `make setup-hooks` | `git config core.hooksPath .githooks`: enable the local `pre-commit` hook, once per clone |
| `make up`, `make down`, `make logs` | Start the stack in Docker (build included), stop it, follow logs |
| `make db-up` | Postgres from compose, with the dev override |
| `make migrate`, `make revision m="..."` | Run Alembic, or autogenerate a revision |
| `make login` | Log the second account in locally and save the session |
| `make login-docker` | The same inside the compose image and volume |
| `make persona-import FILE=...`, `make examples-import FILE=...`, `make examples-stats` | Import the persona text, import style examples from JSON, print counts per tag (see [Persona and style examples](#persona-and-style-examples)) |
| `make examples-convert INPUT=... ME=... \| ME_ID=... [OUT=...]` | Convert a Telegram Desktop JSON chat export into a draft examples file; give exactly one of `ME` and `ME_ID`; needs no `.env` and no database |
| `make reply-preview CONTACT=... [MODE=...]` | Generate one reply for a stored conversation and print it with a stats line; sends nothing, writes nothing (see [Previewing a reply](#previewing-a-reply)) |
| `make outbox-enqueue CONTACT=... TEXT="..." [IN=...]`, `make outbox-list [STATE=...] [LIMIT=...]`, `make outbox-cancel ID=...`, `make outbox-retry ID=... [IN=...] [YES=1]`, `make outbox-status`, `make outbox-resume [YES=1]` | The owner's commands for the outbox (see [The outbox and the sender](#the-outbox-and-the-sender)) |
| `make schedule-import FILE=...`, `make schedule-show`, `make schedule-check [AT="YYYY-MM-DD HH:MM"]`, `make schedule-plan CONTACT=...` | The owner's commands for the schedule profile (see [Humanization and the schedule](#humanization-and-the-schedule)) |
| `make llm-check` | Send one tiny request to DeepSeek with the `reply` settings and print the result (see [Checking the LLM key](#checking-the-llm-key)) |

### Checking the LLM key

The LLM provider is DeepSeek and the key comes from `DEEPSEEK_API_KEY`. Nothing in the app calls it
yet; to check the key, the model name and the network once, run

```sh
make llm-check   # uv run python -m cyrano llm-check
```

It sends a tiny request (system: reply with the single word OK; user: ping) with the `reply`
settings and prints one line, never the reply text:

```
llm-check ok: model=deepseek-chat input_tokens=14 output_tokens=2 latency_ms=620
```

`cache_hit_tokens=...` is added when the provider reports it. On any LLM error it prints
`llm-check failed: <error class>: <message>` to stderr and exits with code 1, for example a
rejected key (`LLMAuthError`, HTTP 401) or an unreachable provider (`LLMUnavailableError`). A request
makes up to 3 retries with a backoff of at most 30 s and an attempt limit of 60 s, so a provider that
never answers can take about four minutes to fail.

### Persona and style examples

The persona (one text that describes the owner) and the style examples (real dialogues that end
with the owner's reply) are imported by a command and stored in PostgreSQL; the app never reads a
file for them. Only `reply-preview` uses them so far. Formats, limits, selection and privacy rules:
[.claude/docs/persona-and-examples.md](.claude/docs/persona-and-examples.md). Samples with invented
content are in `examples/`.

The real files live only in `data/` (ignored by git and Docker): `data/persona.md` and
`data/style-examples.json`. The commands run on the host, like `make migrate`: they need a complete
`.env`, a database that is up (`make db-up`) and the migrations applied (`make migrate`).

```sh
make persona-import FILE=data/persona.md
make examples-import FILE=data/style-examples.json
make examples-stats
uv run python -m cyrano persona show                                   # length and update time, never the text
uv run python -m cyrano examples import data/style-examples.json --replace --yes   # replace everything
```

To start from a chat exported by Telegram Desktop (machine-readable JSON, one chat), convert it into
a draft first. The converter reads a file and writes a file: it needs no `.env`, no database and no
network. Give exactly one way to identify yourself, `--me` (your sender name) or `--me-id` (your
`from_id`, better when the name is awkward to quote in a shell):

```sh
make examples-convert INPUT=data/result.json ME_ID=user1000000001
make examples-convert INPUT=data/result.json ME='Anna' OUT=data/style-examples.json
uv run python -m cyrano examples convert data/result.json --me-id user1000000001 --tags flirty --max 100 --gap-hours 6 --window 6 --redact Annie --force
```

The draft goes to `data/` (other places need `--allow-any-path`), an existing file needs
`--force`, and the output is counts only. Review the draft before importing it: the examples,
the other person's lines included, are sent to the LLM provider inside prompts. Options, redaction
and the review step: [.claude/docs/persona-and-examples.md](.claude/docs/persona-and-examples.md).

Importing the same examples again skips the dialogues already stored. An invalid file imports
nothing and the command prints up to 20 problems as locations and rules (for example
`dialogues[3].turns[1].text: empty`), never the text. Every command exits with 0, or with 1 and a
readable message on an error. `STYLE_EXAMPLES_MAX` limits how many examples one selection returns.

### Previewing a reply

`make reply-preview CONTACT=<contact id> [MODE=<mode name>]` (or
`uv run python -m cyrano reply-preview ID [--mode NAME] [--show-prompt]`) builds the layered prompt
from the persona, the mode, the contact's profile facts, the examples and the last messages of a
stored conversation, asks the LLM for the owner's next message and prints it, followed by one stats
line (model, tokens, examples and messages kept of offered, prompt characters). It lets the owner
judge the style before any autopilot exists. It needs a complete `.env` with `DEEPSEEK_API_KEY`, the
database up and an imported persona. It sends nothing to Telegram and writes nothing to the
database. `--show-prompt` prints the two messages of the prompt first, with a warning: they contain
personal data. Errors print one line with the error class and exit with 1. Layers, budget, cleaning
and what the prompt contains: [.claude/docs/reply-generation.md](.claude/docs/reply-generation.md).

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
| `OWNER_TELEGRAM_ID` | yes | | Telegram id of the main account; the only user the control bot will answer, and the only chat it writes to |
| `DEEPSEEK_API_KEY` | yes | | DeepSeek API key |
| `DEEPSEEK_MODEL` | no | `deepseek-chat` | DeepSeek model name; the model of every LLM step that has none of its own. Must not be blank |
| `DEEPSEEK_BASE_URL` | no | `https://api.deepseek.com` | Base URL of the DeepSeek API; must start with `http://` or `https://` |
| `DEEPSEEK_THINKING` | no | `false` | `true` lets the model reason before it answers; by default thinking is switched off, so a chat reply spends no tokens on reasoning and the temperature applies |
| `LLM_REPLY_MODEL`, `LLM_SUMMARY_MODEL` | no | `DEEPSEEK_MODEL` | Model for reply generation and for summary extraction; must not be blank |
| `LLM_REPLY_TEMPERATURE`, `LLM_SUMMARY_TEMPERATURE` | no | `1.0`, `0.2` | Default sampling temperature of each step, from 0 to 2 |
| `LLM_REPLY_MAX_TOKENS`, `LLM_SUMMARY_MAX_TOKENS` | no | `400`, `1500` | Default limit on the length of each step's reply, a positive integer |
| `DATABASE_URL` | yes | | PostgreSQL URL, must start with `postgresql+asyncpg://` |
| `REDIS_URL` | yes | | Redis URL, must start with `redis://` or `rediss://` |
| `MEDIA_DIR` | no | `media` | Where downloaded media goes: today the profile photos in `profiles/<internal contact id>.jpg` and the Leomatch card photos in `profiles/<internal contact id>-card-<n>.jpg` (n from 1) |
| `LOG_LEVEL` | no | `INFO` | Level of the application log. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, case-insensitive |
| `TELETHON_LOG_LEVEL`, `AIOGRAM_LOG_LEVEL` | no | `WARNING` | Level of the `telethon` and `aiogram` loggers; same allowed values as `LOG_LEVEL` |
| `HTTPX_LOG_LEVEL` | no | `WARNING` | Level of the `httpx` logger, also applied to `httpcore`; same allowed values as `LOG_LEVEL` |
| `TIMEZONE` | no | `Europe/Moscow` | IANA zone name, validated at startup |
| `STYLE_EXAMPLES_MAX` | no | `8` | Most style examples one selection returns, from 1 to 50. Only `reply-preview` selects examples so far |
| `REPLY_HISTORY_MESSAGES` | no | `20` | How many last messages of a contact go into the reply prompt, from 1 to 100 |
| `REPLY_PROMPT_BUDGET_CHARS` | no | `24000` | Character budget of the whole reply prompt, from 4000 to 200000; over it, examples and then the oldest messages are dropped |
| `AUTOPILOT_ENABLED` | no | `false` | `true` allows outbox rows from the autopilot to be sent. The autopilot does not exist yet, and the owner's test commands do not consult this switch; the status check applies to both |
| `SEND_LIMIT_MINUTE` | no | `4` | Most outgoing messages per minute, all contacts together; at least 1, not above `SEND_LIMIT_HOUR` |
| `SEND_LIMIT_HOUR` | no | `40` | Most outgoing messages per hour, all contacts together; at least 1, not above `SEND_LIMIT_DAY` |
| `SEND_LIMIT_DAY` | no | `200` | Most outgoing messages per day, all contacts together; at least 1 |
| `SEND_LIMIT_CONTACT_MINUTE` | no | `2` | Most outgoing messages per minute to one contact; at least 1, not above `SEND_LIMIT_CONTACT_HOUR` or `SEND_LIMIT_MINUTE` |
| `SEND_LIMIT_CONTACT_HOUR` | no | `15` | Most outgoing messages per hour to one contact; at least 1, not above `SEND_LIMIT_CONTACT_DAY` or `SEND_LIMIT_HOUR` |
| `SEND_LIMIT_CONTACT_DAY` | no | `60` | Most outgoing messages per day to one contact; at least 1, not above `SEND_LIMIT_DAY` |
| `HEARTBEAT_PATH` | no | `<tmpdir>/cyrano-heartbeat` | Liveness file touched by the app and read by the app healthcheck |
| `LEOMATCH_BOT_USERNAME` | no | `leomatchbot` | Username of the matching bot whose chat is searched for a new contact's profile text; a leading `@` is allowed. The default is an assumption: check the bot's real username in Telegram |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | for Docker | | Credentials and database name of the compose Postgres; see [Variables](#variables) |
| `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT` | no, Docker only | `5432`, `6379` | Host ports that the override file publishes on `127.0.0.1` |
| `TEST_DATABASE_URL` | for `make test` | | Not an application setting; see [Testing](#testing) |

A relative `USERBOT_SESSION_PATH` or `MEDIA_DIR` is kept as written; when something opens it
(the userbot opens the session path; the profile collector writes photos under the media directory), it is relative to the
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
startup logic, the login flow, the intake on real PostgreSQL, the profile collection (parser, match
check, matching-bot lookup, collector, `update_identity` on real PostgreSQL) and the check that the
userbot's read client cannot send, and for the control bot: owner-only access, the card rendering, the card sender,
the handlers driven through the real dispatcher, the announcer and the Reply and Ignore actions on
real PostgreSQL, and for the LLM client: the request and reply shapes, JSON validation with retries, the retry and deadline rules, the per-step settings and the logs, all against `httpx.MockTransport`, and for the persona and style examples: the file validation, the import and the replace on real PostgreSQL, the selection, the commands, migration `0002` and the sample files, and for the outbox: the repository, the guard and the sender on real PostgreSQL, the limits, the pause and the stop, the adapter, the notifier, the commands and the structural check that only one module can send. Telegram and DeepSeek are always faked: no test connects to either. All tests use fake values only.

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

Run `make setup-hooks` once per clone to enable the local `pre-commit` hook (`.githooks/pre-commit`).
It runs `make check` and then `make test` before every commit, the same commands as CI, and only
reads: it never formats or stages anything. `CYRANO_HOOK_SKIP_TESTS=1 git commit ...` skips only the
tests (they need PostgreSQL, see [Testing](#testing)), and `git commit --no-verify` skips the hook.
See [CONTRIBUTING.md](CONTRIBUTING.md#git-hooks).

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
- [.claude/docs/persona-and-examples.md](.claude/docs/persona-and-examples.md)
- [.claude/docs/reply-generation.md](.claude/docs/reply-generation.md)
- [.claude/docs/outbox-and-sender.md](.claude/docs/outbox-and-sender.md)
- [.claude/docs/humanization.md](.claude/docs/humanization.md)
- [.claude/docs/safety-rules.md](.claude/docs/safety-rules.md)
- [.claude/docs/decisions.md](.claude/docs/decisions.md)
