# Outbox and sender

Exists since CYR-19. The outbox is the table of messages waiting to go out; the sender is the one
piece of code that sends them to contacts. The Telethon session lives in the app process, so sending
stays there. A future Arq worker (CYR-17) will only generate replies and write them to the outbox.

**The autopilot does not exist.** The only way to put a row in the outbox today is the owner's
explicit test command. Nothing is ever sent automatically. Rules S1 and S2:
[safety-rules.md](safety-rules.md). Reasons: [decisions.md](decisions.md).

## The table

`scheduled_messages`, migration `0003`:

| Column | Holds |
| --- | --- |
| `id` | Internal id; the one shown to the owner and in logs |
| `contact_id` | Foreign key to `contacts` |
| `trigger_message_id` | The incoming message this row answers, or `NULL`; plain integer, no foreign key |
| `text` | The text to send. Personal data: never logged, printed or put in a notification |
| `source` | `autopilot` or `manual_test` |
| `send_at` | When the row is due, `timestamptz` |
| `state` | See below; starts as `scheduled` |
| `attempts` | Unused and always 0; it stays in the schema, and the code does not read it |
| `error_class` | `uncertain`, `interrupted`, the class name of a certain refusal or of a flood wait, or `NULL` |
| `cancel_reason` | A reason constant, or `NULL` |
| `claimed_at` | When the sender claimed the row |
| `sent_tg_message_id` | The Telegram id of the sent message |
| `created_at`, `updated_at` | `timestamptz` |

Constraints: `text` not empty, `state` and `source` from the allowed values. Indexes: a partial
unique index on `(contact_id, trigger_message_id) WHERE trigger_message_id IS NOT NULL`, so one
incoming message yields at most one row, and an index on `(state, send_at)` for the claim. Enqueue
with a known trigger returns the row already stored, also when two calls race.

## States

| From | To | Who | When |
| --- | --- | --- | --- |
| (none) | `scheduled` | `OutboxService.enqueue` | Text and guard passed |
| `scheduled` | `sending` | `claim_due` in the sender | The row is due |
| `scheduled` | `cancelled` | `cancel_scheduled` (the owner's command), `cancel_pending_for_contact` (no caller yet; for CYR-20, CYR-21) | |
| `sending` | `sent` | `complete_send` in the sender | Telegram accepted the message |
| `sending` | `scheduled` | `reschedule` in the sender | Flood wait, and nothing else |
| `sending` | `failed` | `mark_failed` in the sender | Certain refusal, unknown outcome, or `interrupted` |
| `sending` | `cancelled` | `mark_cancelled` in the sender | The guard refused the row at send time |
| `failed` | `scheduled` | `retry_failed`, only from the owner's `outbox retry --yes` | The owner checked the chat and decided |

`sent` and `cancelled` are final. `failed` is final for the sender: `claim_due` and `reschedule`
never touch it, and only the owner's retry command takes a row back. `complete_send` has no state
check on purpose: it records what happened in Telegram, so a message that did go out is always
stored as sent.

## Claim and locking

`claim_due(now)` claims **one** row: state `scheduled`, `send_at <= now`, ordered by `send_at` then
`id`, `FOR UPDATE SKIP LOCKED`. In the same transaction it sets `sending` and `claimed_at`. Two
claimers never get the same row, and a `sending` row is never returned. There is one sender loop in
the app; the locking makes a second one harmless, and a test runs two.

`complete_send(id, contact_id, text, tg_message_id, sent_at)` is one transaction: it appends the
outgoing message idempotently and marks the row sent. It does not touch `contacts.last_message_at`,
which stays "the last message from the contact".

## The guard

`services/send_guard.py`: the pure `check(contact, row_source, settings)` and the thin `SendGuard`.

| Contact | Source | `AUTOPILOT_ENABLED` | Decision |
| --- | --- | --- | --- |
| missing | any | any | refused, `unknown_contact` |
| not `active` | any | any | refused, `contact_not_active` |
| `active` | `autopilot` | `false` | refused, `autopilot_disabled` |
| `active` | `autopilot` | `true` | allowed |
| `active` | `manual_test` | any | allowed |

It runs twice: in `OutboxService.enqueue`, which also validates the text (NUL removed, trimmed, not
empty, at most `MAX_OUTBOX_TEXT_LENGTH`, 4096 UTF-16 code units, which is how Telegram counts, so
2048 emoji fit and 2049 do not) and raises `EnqueueRefused` without storing anything;
and in the sender, right before sending, on a contact it has just re-read. A refusal at send time
cancels the row with the reason as `cancel_reason`, and nothing is sent. A row the owner retried goes
through the same check.

## The sender

`services/outbox_sender.py`, `OutboxSender`, a background task of the app. Its module is the only
one in `src` that calls `send_text`. Strictly sequential: one row at a time, one row per poll.

| Constant | Value | Meaning |
| --- | --- | --- |
| `SENDER_POLL_INTERVAL` | 2 s | Pause between polls |
| `SENDER_STALE_CHECK_INTERVAL` | 60 s | How often stuck rows are looked for |
| `SENDING_STALE_AFTER` | 120 s | Age of a claim that counts as stuck |
| `SENDER_SEND_DEADLINE` | 100 s | Deadline of one send; shorter than the stale age |
| `FLOOD_WAIT_MARGIN` | 5 s | Added to the wait Telegram asks for |
| `FLOOD_WAIT_NOTIFY_ABOVE` | 60 s | A longer wait notifies the owner |

Each poll: claim one row, re-read the contact, run the guard, `send_text`, `complete_send`.

### Errors

The Telethon adapter (`userbot/adapter.py`) maps every Telegram failure to one of three classes.
They carry a class name, and the seconds for a flood wait, never a text.

| Class | Meaning | Telethon errors |
| --- | --- | --- |
| `SendFloodWait` | Telegram explicitly refused and said how long to wait | `FloodWaitError`, `FloodPremiumWaitError`, `FloodTestPhoneWaitError`, `SlowModeWaitError` |
| `SendRejected` | Certainly not delivered, and it cannot improve | `ForbiddenError` and its subclasses (for example `UserPrivacyRestrictedError`, `ChatWriteForbiddenError`), `InputUserDeactivatedError`, `MessageTooLongError`, `PeerIdInvalidError`, `PeerIdNotSupportedError`, `UserBlockedError`, `UserIdInvalidError`, `UserIsBlockedError`, `YouBlockedUserError`; a `ValueError` from the peer lookup (`PeerNotFound`) |
| `SendUncertain` | The outcome is unknown | Everything else: timeouts, connection errors, generic RPC and server errors, a `ValueError` from `send_message`, `PeerFloodError`, `UserDeactivatedError`, `UserDeactivatedBanError`, a send that returned no message (`MissingSentMessage`) |

| Result of the send | What the sender does | Stored `error_class` | Owner notified |
| --- | --- | --- | --- |
| `SendFloodWait` | Reschedule at now + wait + margin | The class name | Only above 60 s |
| `SendRejected` | `failed` | The class name | Once |
| `SendUncertain` | `failed` | `uncertain` | Once, and told to check the chat and how to retry |
| Any other exception | Same as `SendUncertain` | `uncertain` | Once |
| The send deadline passed | Same as `SendUncertain` | `uncertain` | Once |
| Storing the failure or `complete_send` failed | The row stays `sending`; the stale check fails it | `interrupted` | Once, by the stale check |

**Only a flood wait is rescheduled automatically.** A flood wait is the one failure where Telegram
says the message was refused and when to try again. Every other failure ends the row: the transport
is called exactly once for it, and an unknown outcome is never retried by the sender, because the
first attempt may have reached Telegram and each new call gets a new `random_id`, so the contact would
receive the message twice. A cause of an uncertain row (a class name, or `SendDeadlinePassed`) goes to
the log line; the row stores `uncertain`. The notification carries the two internal ids only.

`PeerFloodError`, `UserDeactivatedError` and `UserDeactivatedBanError` concern the owner's own
account, not the contact. Each row they hit fails on its own; stopping the whole sender belongs to
CYR-22.

Telethon sleeps through flood waits up to its `flood_sleep_threshold` (60 s by default) inside the
call, so in production `SendFloodWait` normally means more than 60 s.

### After an uncertain failure

The owner reads the chat on the second account. If the message arrived, nothing is left to do: the
row stays `failed`. If it did not, `outbox retry ID --yes` sets the row back to `scheduled`, and the
sender handles it like any other row: one call, and the guard first. The command also accepts rows
that failed as `interrupted` or with a certain refusal.

### A stuck row is never sent again

A row in `sending` may or may not have reached Telegram, and sending it again could write to a
person twice. Such a row is therefore never retried:

- **Startup sweep.** Once, before the first poll: every `sending` row left by the previous process
  becomes `failed` with `error_class = interrupted`, and the owner gets one notification per row, told
  to check the chat and how to retry.
- **Stale check.** Every 60 s: `sending` rows claimed more than 120 s ago go the same way.
- **Cancellation mid-send** (shutdown) leaves the row in `sending`, for the next startup sweep.

The owner decides what to do after reading the chat. A step that fails (a database outage) is logged
with the class name, and the loop goes on: one bad row never stops it. Only cancellation does.

## The transport, the notifier and the adapter

- `MessageTransport` (`services/message_transport.py`) has exactly one method:
  `send_text(tg_id, text) -> SentMessage`.
- `OwnerNotifier` (`services/owner_notifier.py`) has exactly one method: `notify(text)`.
- `TelethonAdapter` (`userbot/adapter.py`) is the only module that touches the Telethon client
  directly and the only one with a Telethon write call: `send_message` with plain text, no parse
  mode and no link preview. It also implements the read Protocol `UserbotClient`, whose generic
  request call was replaced by `get_full_user(tg_id)`; only the adapter imports a request class
  (`GetFullUserRequest`). It logs nothing.
- `TelegramOwnerNotifier` (`bot/notifier.py`) sends one plain Russian message to the owner chat. The
  owner id comes from `Settings`, so no caller can address another chat. The texts are in
  `bot/messages.py` and reach the sender as templates (`SenderNotices`), because `services` cannot
  import `bot`. One attempt: a failure is logged with the class name and changes nothing.

## Two sources and the switch

| Source | Written by | Needs `AUTOPILOT_ENABLED` | Status check |
| --- | --- | --- | --- |
| `manual_test` | The owner's `outbox enqueue` command | No | Yes |
| `autopilot` | CYR-17, not built | Yes | Yes |

`AUTOPILOT_ENABLED` defaults to `false` and stays off until the guards listed in
[decisions.md](decisions.md) exist. Turning it on today changes nothing visible, because nothing
writes `autopilot` rows. It does not switch the sender off: the sender always runs.

## Owner commands

`python -m cyrano outbox ...` runs on the host like `make migrate`, with the app `Settings` and the
database up. The sending itself is done by the running app.

| Command | Makefile | What it does |
| --- | --- | --- |
| `outbox enqueue CONTACT_ID TEXT [--in-seconds N]` | `make outbox-enqueue CONTACT=... TEXT="..." [IN=...]` | Adds a `manual_test` row, due in N seconds (default 5, 0 to 86400). Prints the row id and the planned time |
| `outbox list [--state S] [--limit N]` | `make outbox-list [STATE=...] [LIMIT=...]` | Newest first (default 50, 1 to 1000): id, contact id, source, state, `send_at`, error class. Never a text |
| `outbox cancel ID` | `make outbox-cancel ID=...` | Cancels a `scheduled` row. A row in any other state is refused with a message |
| `outbox retry ID [--in-seconds N] [--yes]` | `make outbox-retry ID=... [IN=...] [YES=1]` | Only for a `failed` row; any other state is refused with a message. Without `--yes` it prints a reminder to check the chat first and changes nothing. With `--yes` it sets `scheduled`, `send_at` to now + N seconds (default 5, 0 to 86400), clears `error_class` and `cancel_reason`, and prints the row id and the planned time |

Any error prints one readable line and exits with 1, a bad command line included; an invalid
configuration exits with 2 like every command. A text that starts with `-` goes after `--`. The text
is never printed back, not even in a usage error.

## Privacy

Rule S9. Logs, notifications, command output and errors carry internal ids, states, counters and
class names. They never carry a message text, a name, a username, a Telegram id or a photo path.
`ScheduledMessage.text` is excluded from the model's `repr`. A driver error is never passed on with
its message, because PostgreSQL can quote row values in it.

## Not built yet

| What | Ticket |
| --- | --- |
| The autopilot: generating replies and writing `autopilot` rows | CYR-17 |
| Humanization: delay, typing action, splitting, quiet hours | CYR-18 |
| Stop-triggers, which will use `cancel_pending_for_contact` | CYR-20 |
| Manual pause from the owner's own messages | CYR-21 |
| Central rate limits, and stopping the sender when the own account is restricted or lost | CYR-22 |
