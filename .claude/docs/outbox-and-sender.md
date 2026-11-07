# Outbox and sender

Exists since CYR-19; the limits, the pause and the stop since CYR-22; the pre-send part and the
availability check since CYR-18. The outbox is the table of
messages waiting to go out; the sender is the one piece of code that sends them to contacts. The
Telethon session lives in the app process, so sending stays there. The Arq worker (CYR-17) only
generates replies and writes them to the outbox ([autopilot.md](autopilot.md)).

Two things write rows: the owner's explicit test command (`manual_test`) and, since CYR-17, the
autopilot job (`autopilot`), which runs only with `AUTOPILOT_ENABLED=true`. Rules S1 and S2:
[safety-rules.md](safety-rules.md). Reasons: [decisions.md](decisions.md).

## The table

`scheduled_messages`, migration `0003`, `due_at` since migration `0005`:

| Column | Holds |
| --- | --- |
| `id` | Internal id; the one shown to the owner and in logs |
| `contact_id` | Foreign key to `contacts` |
| `trigger_message_id` | The incoming message this row answers, or `NULL`; plain integer, no foreign key |
| `text` | The text to send. Personal data: never logged, printed or put in a notification |
| `source` | `autopilot` or `manual_test` |
| `send_at` | When the sender may claim the row, `timestamptz`. A flood wait, a reached limit or a closed window moves it forward |
| `due_at` | The originally intended send time, `timestamptz`, not null. Set at enqueue to the `send_at` given there and never changed by a reschedule; only the owner's `retry_failed` resets it. The stale rule counts from it |
| `state` | See below; starts as `scheduled` |
| `attempts` | Unused and always 0; it stays in the schema, and the code does not read it |
| `error_class` | `uncertain`, `interrupted`, `rate_limited`, `outside_hours`, the class name of a certain refusal, of a flood wait or of an account problem, or `NULL` |
| `cancel_reason` | A reason constant, or `NULL` |
| `claimed_at` | When the sender claimed the row |
| `sent_tg_message_id` | The Telegram id of the sent message |
| `created_at`, `updated_at` | `timestamptz` |

Constraints: `text` not empty, `state` and `source` from the allowed values. Indexes: a partial
unique index on `(contact_id, trigger_message_id) WHERE trigger_message_id IS NOT NULL`, so one
incoming message yields at most one row, and an index on `(state, send_at)` for the claim. Enqueue
with a known trigger returns the row already stored, also when two calls race. Every `autopilot` row
carries the incoming message it answers as `trigger_message_id` (the internal `messages.id`); the job
looks for a row with that key (`has_row_for_trigger`, any state) before it generates, so a job that
runs twice, a retried job and the sweeper never produce a second row or a second model call.

## States

| From | To | Who | When |
| --- | --- | --- | --- |
| (none) | `scheduled` | `OutboxService.enqueue` | Text and guard passed |
| `scheduled` | `sending` | `claim_due` in the sender | The row is due |
| `scheduled` | `cancelled` | `cancel_scheduled` (the owner's command), `cancel_pending_for_contact` (the sender after a recipient rejection, reason `send_rejected`; the manual-message detector, reason `manual_message`, CYR-21; the hand-over on a stop-trigger, reason `stop_trigger`, CYR-20) and `cancel_superseded_autopilot_rows` (the reply job, the contact's older `autopilot` rows, reason `superseded`, CYR-41) | |
| `sending` | `sent` | `complete_send` in the sender | Telegram accepted the message |
| `sending` | `scheduled` | `reschedule` in the sender | Flood wait, a reached limit or an autopilot row inside a window, and nothing else |
| `sending` | `failed` | `mark_failed` in the sender | Certain refusal, account problem, unknown outcome, or `interrupted` |
| `sending` | `cancelled` | `mark_cancelled` in the sender | The row is stale, the guard refused it at send time, or a newer message superseded it (`superseded`, CYR-41) |
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
and in the sender, on a contact it has just re-read: once after the claim, so a refused contact is
never marked as read, and once more after the pre-send part, right before `send_text`, because that
part can take over a minute. A refusal at send time cancels the row with the reason as
`cancel_reason`, and nothing is sent. A row the owner retried goes through the same check.

**Relation to a manual message (CYR-21).** When the owner writes to a contact by hand, the manual-message
detector pauses the contact and cancels its `scheduled` rows with the reason `manual_message`
(`cancel_pending_for_contact`). It cannot touch a row in state `sending`: the claim and the send call
belong to the sender. That row is stopped by the second guard check above, which re-reads the contact
and refuses it with `contact_not_active` because the contact is `paused` now. The detector also uses
the `sending` state to tell the sender's own echo from a manual message: if the contact has a
`sending` row and the message id is not stored yet, it waits (0.25 to 8 s, with early exit) for the
sender's `complete_send` to store the id. The sender and the detector share nothing else; the
detector never calls the transport. A message the owner wrote by hand is stored as an outgoing
message and so counts toward the send limits.

## The sender

`services/outbox_sender.py`, `OutboxSender`, a background task of the app. Its module is the only
one in `src` that calls the transport. Strictly sequential: one row at a time, one row per poll.

| Constant | Value | Meaning |
| --- | --- | --- |
| `SENDER_POLL_INTERVAL` | 2 s | Pause between polls |
| `SENDER_STALE_CHECK_INTERVAL` | 60 s | How often stuck rows are looked for |
| `SENDING_STALE_AFTER` | 195 s | Age of a claim that counts as stuck: `LONGEST_PRE_SEND + SENDER_SEND_DEADLINE + SENDING_STALE_MARGIN` |
| `SENDER_SEND_DEADLINE` | 100 s | Deadline of one send; shorter than the stale age |
| `PRE_SEND_DEADLINE_MARGIN` | 15 s | Added to the profile's `typing.max_seconds` for the deadline of the pre-send part; the read pause (at most 10 s) fits inside it |
| `LONGEST_PRE_SEND` | 75 s | The longest legal pre-send part: `MAX_TYPING_SECONDS` (60 s) plus the margin |
| `SENDING_STALE_MARGIN` | 20 s | Keeps a healthy claim from ever counting as stuck; a test asserts the relation |
| `FLOOD_WAIT_MARGIN` | 5 s | Added to the wait Telegram asks for |
| `FLOOD_WAIT_NOTIFY_ABOVE` | 60 s | A longer wait notifies the owner |
| `ROW_STALE_AFTER_SECONDS` | 6 h | A row whose `due_at` is older than this is cancelled, not sent |
| `LIMIT_NOTICE_INTERVAL_SECONDS` | 1 h | At most one limit notice per kind in this time |
| `STALE_NOTICE_INTERVAL_SECONDS` | 1 h | At most one stale summary notice in this time |
| `STALE_NOTICE_CONTACT_IDS_MAX` | 5 | Contact ids listed in a stale summary notice |
| `STOP_REMINDER_INTERVAL_SECONDS` | 6 h | Time between reminders while the sender is stopped |
| `OWNER_NOTIFY_DEADLINE` | 15 s | Deadline of one owner notification |

Each poll, in this order:

1. Report waiting stale cancels if their notice is due (see the stale rule). Read `sender_state`. If
   it is stopped, send the stop reminder if it is due (see the sender state). Stopped, or
   `paused_until` in the future: nothing is claimed, and the transport is not called.
2. Claim one row.
3. Stale: `due_at` more than 6 h ago cancels the row with reason `stale`, counts it for the stale
   summary notice, and sends that notice if it is due.
4. Re-read the contact and run the guard.
5. Check the limits. A reached limit puts the row back; see below.
6. Autopilot rows only: the availability check. See below.
7. Autopilot rows only: the supersession check. See below.
8. The pre-send part: `mark_read`, a pause, `show_typing`. See below.
9. Re-read the contact and run the guard again, then the supersession check again.
10. `send_text`, then `complete_send`.

The startup sweep and the stale check of stuck rows keep running while the sender is paused or
stopped. They only ever mark rows as failed; they send nothing.

If `sender_state` cannot be read, nothing is claimed, and the step logs the class name. If the limits
cannot be read after the claim, nothing is sent and the row stays `sending`, for the stale check. The
same goes for a schedule profile that cannot be read.

### The availability check

`ScheduleService.blocked_until(now)` with the stored or the default profile. An `autopilot` row inside
a night or busy window goes back to `scheduled` at the window's end plus
`uniform(postponed_spread_minutes)`, with `error_class = outside_hours`. It is never dropped and not
an attempt, because the transport was not called. `due_at` stays, and the owner gets no notice. A spread
that lands in the next window is held again at the next claim. A `manual_test` row skips the check: the
owner asked for that message now.

The stale rule runs first, so a row held past 6 h from its `due_at` is cancelled as `stale` when it
comes back, as for a limit. Why a planned row does not run into this:
[humanization.md](humanization.md#the-safety-net).

### The supersession check

CYR-41. For rows of source `autopilot` only; a `manual_test` row is never superseded, and neither is a
row without a `trigger_message_id`. A row is superseded when the latest message of its contact, by
`(created_at, id)` as everywhere else ([Message times](contact-lifecycle.md#message-times)), is newer
than its trigger message. Any newer message counts, incoming (the contact wrote again) or outgoing (the
owner or the sender answered). An older message that a catch-up stored later, with a larger id, does not.

The check runs twice: after the availability check and before `mark_read`, and again after the pre-send
part together with the second guard check, right before `send_text`, so a message that arrives during
the read pause or the typing still cancels the send. A superseded row becomes `cancelled` with the reason
`superseded` (`CANCEL_REASON_SUPERSEDED`). The owner gets no notice and nothing counts as an attempt: the
transport was not called for the send. The first check logs the INFO line `outbox row superseded by a
newer message` (ids only) and no step lines; the second one logs the same line and the `send` step as
`skipped`. A row that was already sent is never touched: only a row the sender has claimed (`sending`)
is cancelled, and `sent` is final.

Why: the reaction delay (20 to 120 s) is longer than the debounce window (12 s), so the job of an earlier
message may have written its row before the next message arrived. Without this check every message
that came after an earlier job ran got its own reply. The reply job cancels such rows too, see
[autopilot.md](autopilot.md#one-reply-per-burst); the sender check covers the gap before that job runs,
a row of a job that failed or lags, and the time spent in the pre-send part.

### The pre-send part

For every source, in this order: `mark_read(tg_id)`, a pause of `uniform(read_pause_seconds)`,
`show_typing(tg_id, typing_seconds(text))`. The numbers come from the profile; the rules are in
[humanization.md](humanization.md).

It runs under one deadline, the profile's `typing.max_seconds` + `PRE_SEND_DEADLINE_MARGIN`.

| What happens in the pre-send part | What the sender does |
| --- | --- |
| `mark_read` or `show_typing` fails with anything else | Logs the class name and goes on; the send still happens |
| `SendFloodWait` | Exactly as a flood wait on a send: pauses the sender, reschedules the row, same notice rule. Nothing is sent |
| `SendAccountBlocked` | Exactly as on a send: stops the sender, the row is `failed` with the kind, nothing is sent |
| The deadline passes | Logs it, skips the rest of the pre-send part and goes on to the guard and the send |
| Cancellation (shutdown) | Leaves the row in `sending`, for the next startup sweep, as for a send |
| The guard refuses after it | The row is cancelled with the reason; nothing is sent |

### Reading the logs

Since CYR-38 every row that reaches the pre-send part gets exactly four INFO lines with the message
`outbox row step` from the logger `cyrano.services.outbox_sender`, one per step, in this order:
`mark_read`, `pause`, `typing`, `send`. The `send` line comes before the `outbox row sent` line,
which is unchanged. A row that stops earlier (stale, refused by the first guard, held by a limit or a
window, an unreadable profile) has only its own line and no step lines.

| Field | Holds |
| --- | --- |
| `step` | `mark_read`, `pause`, `typing` or `send` |
| `outcome` | `ok`; `failed`; `timeout` (the pre-send deadline or the send deadline ran out in this step); `skipped` (the step did not run: after a timeout, a flood wait or an account error, or `send` after a guard refusal) |
| `error_class` | Only on `failed`: the exception class name, for example `SendUncertain`, `SendFloodWait`, `SendAccountBlocked` |
| `duration_ms` | Integer, measured with the sender's clock; `0` for `skipped` |
| `row_id`, `contact_id` | The internal ids |
| `planned_seconds` | Only on `pause` and `typing`: the seconds drawn or computed for this row |

Nothing else is in a step line: no text, name, username, Telegram id, photo path, and no value of
the schedule profile other than the two planned numbers. A failed `mark_read` or `typing` still
leads to `send` with `ok`, as described above. A flood wait or an account error is logged as
`failed` with its class, the rest as `skipped`, and then handled as in the table above.

A normal sequence, with invented ids:

```
{"ts": "2026-11-05T12:00:00.310+00:00", "level": "INFO", "logger": "cyrano.services.outbox_sender", "msg": "outbox row step", "row_id": 41, "contact_id": 7, "step": "mark_read", "outcome": "ok", "duration_ms": 250}
{"ts": "2026-11-05T12:00:03.161+00:00", "level": "INFO", "logger": "cyrano.services.outbox_sender", "msg": "outbox row step", "row_id": 41, "contact_id": 7, "step": "pause", "outcome": "ok", "duration_ms": 2850, "planned_seconds": 2.8497305032518905}
{"ts": "2026-11-05T12:00:18.362+00:00", "level": "INFO", "logger": "cyrano.services.outbox_sender", "msg": "outbox row step", "row_id": 41, "contact_id": 7, "step": "typing", "outcome": "ok", "duration_ms": 15200, "planned_seconds": 15.2}
{"ts": "2026-11-05T12:00:18.863+00:00", "level": "INFO", "logger": "cyrano.services.outbox_sender", "msg": "outbox row step", "row_id": 41, "contact_id": 7, "step": "send", "outcome": "ok", "duration_ms": 500}
{"ts": "2026-11-05T12:00:18.871+00:00", "level": "INFO", "logger": "cyrano.services.outbox_sender", "msg": "outbox row sent", "row_id": 41, "contact_id": 7}
```

`ts` is written when a step ends, so a step started at `ts - duration_ms`. Above, the read mark was
accepted by Telegram at 12:00:00.310, the pause ran until 12:00:03.161, the typing was held from then
until 12:00:18.362, and the message was accepted half a second later. Group the lines by `row_id`;
the sender handles one row at a time, so the lines of one row are never interleaved with another's.

**A failed read mark or a late display.** When the contact's client shows the read mark only after the
message, look at the `mark_read` line of that row. `ok` with a `ts` before the `typing` and `send`
lines means Telegram accepted the read mark first, so the late display is on the contact's side.
`failed` (with its class) or `skipped` means the read mark was not sent. `timeout` means the call did
not finish within the deadline, so it is unknown whether Telegram got it.

## The limits

`services/send_limiter.py`. All six are optional settings, and each must be at least 1.

| Setting | Default | Counts |
| --- | --- | --- |
| `SEND_LIMIT_MINUTE` | 4 | All outgoing messages in the last 60 s |
| `SEND_LIMIT_HOUR` | 40 | All outgoing messages in the last 3600 s |
| `SEND_LIMIT_DAY` | 200 | All outgoing messages in the last 86400 s |
| `SEND_LIMIT_CONTACT_MINUTE` | 2 | One contact's outgoing messages in the last 60 s |
| `SEND_LIMIT_CONTACT_HOUR` | 15 | One contact's outgoing messages in the last 3600 s |
| `SEND_LIMIT_CONTACT_DAY` | 60 | One contact's outgoing messages in the last 86400 s |

Within a scope, minute must not be above hour and hour must not be above day. A per-contact value
must not be above the global value of the same window. A broken rule is a readable `ConfigError` that
names the variables and never echoes a value.

**The rule.** For every limit, read `outgoing_times(now - window, limit, contact_id or None)`, the most
recent timestamps of stored outgoing messages, at most `limit` of them, newest first. Fewer than
`limit` timestamps: the limit is not reached. Otherwise the slot frees at the oldest of them plus the
window. A time equal to the boundary counts as free, so a message exactly one window old does not
count. The answer for the row is the **latest** of the times over every reached limit.
`slot_frees_at` and `decide` are pure functions and are tested on their own.

The counters come from `messages`, not from Redis, so they survive a restart and count what really
went out. A send with an unknown outcome stores no message and therefore does not count.

**A reached limit.** The row goes back to `scheduled` at the returned time, with
`error_class = rate_limited`. The limiter itself never drops it, and it is not an attempt: the transport was
not called. `complete_send` clears the error class. `reschedule` moves `send_at` and leaves `due_at`
alone, so the stale rule still measures from the original time: a row held by a limit for more than
6 h past its `due_at`, by a daily limit for example, is cancelled as `stale` when it comes back, and
the owner is told in the stale summary notice.

**Notices.** An hourly or a daily limit notifies the owner at most once per hour per kind. A kind is
a scope and a window, so there are four. A second contact reaching the same per-contact limit within
that hour gets no notice of its own. A minute limit never notifies. The dedupe is in memory, so a
restart may repeat a notice.

## Sender state

Table `sender_state`, migration `0004`: exactly one row, `id` fixed to 1 by a CHECK constraint and
seeded idempotently by the migration. Columns: `paused_until`, `pause_reason`, `stopped`, `stop_reason`,
`updated_at`. Both survive a restart because they live in the database.

| Method | Effect | Returns | Caller |
| --- | --- | --- | --- |
| `get` | Reads the row | The state | The sender, `outbox status` |
| `pause_until(until, reason, *, now)` | Only ever extends a pause | Whether no pause was active at `now` | Only the flood path of the sender |
| `stop(reason)` | Sets the stop; the first reason stays | Whether the sender was newly stopped | Only the account path of the sender |
| `resume(*, keep_stop=False)` | Clears the pause and the stop; with `keep_stop` a stopped sender stays exactly as it is | The state before the call | Only `outbox resume` |

A missing row raises `SenderStateMissing`, and the sender fails closed: it claims nothing.

**A pause is sender-wide.** A flood wait is about the account, not one contact, so no contact is sent
to until `paused_until`. A pause ends by itself at that exact time. A shorter wait never shortens a
longer stored pause, and the row goes back to the time computed from its own wait. The sender does
not claim it before the pause ends, whichever is later.

**A stop needs the owner.** An account problem stops all sending until `outbox resume --yes`. It does
not end by itself. Other rows stay `scheduled` and untouched; they are not claimed while stopped. A
long pause or stop can age queued rows past the stale limit: they are cancelled as `stale` when
sending resumes, not sent hours late.

**A stop is not forgotten.** While the sender is stopped, the owner gets a short reminder naming the
kind and the resume command every `STOP_REMINDER_INTERVAL_SECONDS` (6 h), counted from the stop notice
of the current stop. The state is in memory: after a restart during a stop the clock starts at the
first poll, without a notice, so a restart delays the next reminder and never doubles the stop notice.
A resume clears the clock, so no reminder follows it; a new stop starts a new clock. A pause gets no
reminder, because it ends by itself. A failed or hung notice is not repeated at once: the next try is
one interval later. The reminder carries the kind only, no text, no id.

## The stale rule

A row whose `due_at` is more than 6 h in the past when it is claimed is cancelled with reason
`stale`, and nothing is sent. Exactly 6 h is still sent. A reply that is hours late reads worse than
none, and rows can pile up during a stop. The check runs before the guard and before the limits. The
log has a warning with the internal ids, `outbox status` counts the cancelled rows, and `outbox list`
shows the reason.

**The stale summary notice.** A stale cancel is also told to the owner, but never one message per
row, so that a long stop does not end in a burst of notices. The sender counts the cancels in memory
and sends one notice with the **number** of rows cancelled since the previous notice and a short list
of their contact ids (distinct, at most `STALE_NOTICE_CONTACT_IDS_MAX`, 5). The first stale cancel
sends it at once; after that at most one notice per `STALE_NOTICE_INTERVAL_SECONDS` (1 h). Cancels
that arrive inside the hour wait and are sent when the hour has passed, at the next poll, even if no
new stale row comes. Nothing else is in it: no text, no name, no Telegram id. A restart may repeat or
lose a count. A notifier that fails or hangs (the 15 s deadline) changes nothing about the
cancellation, and that notice is not sent again.

The age counts from `due_at`, not from `send_at`, so a reschedule does not restart it. Claiming still
goes by `send_at`.

| What happens to the row | `send_at` | `due_at` |
| --- | --- | --- |
| `enqueue` | The given time | The same time |
| A reached limit, a flood wait or a closed window (`reschedule`) | Moved forward | Unchanged |
| The owner's `outbox retry --yes` (`retry_failed`) | Now + N seconds | The same new time, because the owner wants it sent now |

Rows that existed before migration `0005` got `due_at = send_at`. A row already moved by a reschedule
at that moment counts from the moved time, because the original one was not recorded.

### Errors

The Telethon adapter (`userbot/adapter.py`) maps every Telegram failure to one of four classes, with
the same function for `send_text`, `show_typing` and `mark_read`.
They carry a class name, and the seconds for a flood wait, never a text. The checks run in the order
of the table. The account check comes before the refusals because `UserRestrictedError`, a
restriction of our own account, is a subclass of `ForbiddenError`, which otherwise means the
recipient. The flood branch lists its four classes one by one and never `FloodError`, so
`FrozenMethodInvalidError`, a `FloodError` subclass, is not taken for a flood wait; the account
tuple lists the bare `UnauthorizedError` after its subclasses. Tests pin all of this. The kind stored
and shown is the Telethon class name, never the base class of a subclass.

| Class | Meaning | Telethon errors |
| --- | --- | --- |
| `SendFloodWait` | Telegram explicitly refused and said how long to wait | `FloodWaitError`, `FloodPremiumWaitError`, `FloodTestPhoneWaitError`, `SlowModeWaitError` |
| `SendAccountBlocked(kind)` | The owner's own account is restricted or lost | `PeerFloodError`, `UserDeactivatedError`, `UserDeactivatedBanError`, `UserRestrictedError`, `AuthKeyUnregisteredError`, `AuthKeyInvalidError`, `SessionRevokedError`, `SessionExpiredError`, `FrozenMethodInvalidError`, `AuthKeyDuplicatedError`, `AuthKeyPermEmptyError`, `ActiveUserRequiredError`, `SessionPasswordNeededError`, and a bare `UnauthorizedError` (so any other `UnauthorizedError` subclass) |
| `SendRejected(reason=content)` | Certainly not delivered, because of this text | `MessageTooLongError` |
| `SendRejected(reason=recipient)` | Certainly not delivered, because of this contact | `ForbiddenError` and its other subclasses (for example `UserPrivacyRestrictedError`, `ChatWriteForbiddenError`; also `PremiumAccountRequiredError`, `SensitiveChangeForbiddenError` and `NotAllowedError`, which are recipient-level by default and to be revisited if seen live), `InputUserDeactivatedError`, `PeerIdInvalidError`, `PeerIdNotSupportedError`, `UserBlockedError`, `UserIdInvalidError`, `UserIsBlockedError`, `YouBlockedUserError`; a `ValueError` from the peer lookup (`PeerNotFound`) |
| `SendUncertain` | The outcome is unknown | Everything else: timeouts, connection errors, generic RPC and server errors, a `ValueError` from `send_message`, a send that returned no message (`MissingSentMessage`) |

| Result of the send | What the sender does | Stored `error_class` | Owner notified |
| --- | --- | --- | --- |
| `SendFloodWait`, also from `mark_read` or `show_typing` | Pauses the whole sender until now + wait + margin, and reschedules the row to that time | The class name | Once per new pause, only above 60 s |
| `SendAccountBlocked`, also from `mark_read` or `show_typing` | Stops the sender; `failed`; other rows untouched; the contact is not paused | The kind | Once, when newly stopped; names the kind and the resume command; then a reminder every 6 h while stopped |
| `SendRejected`, recipient | `failed`; contact `active -> paused`; the contact's other `scheduled` rows cancelled with reason `send_rejected` | The class name | Once, with the contact id and the class name |
| `SendRejected`, content | `failed`, and nothing else | The class name | Once |
| `SendUncertain` | `failed` | `uncertain` | Once, and told to check the chat and how to retry |
| Any other exception | Same as `SendUncertain` | `uncertain` | Once |
| The send deadline passed | Same as `SendUncertain` | `uncertain` | Once |
| Storing the failure or `complete_send` failed | The row stays `sending`; the stale check fails it | `interrupted` | Once, by the stale check |
| A newer message than the trigger exists (`autopilot` rows with a trigger only) | `cancelled`, reason `superseded`; nothing is sent, nothing counted | none | None |
| The row is stale when claimed | `cancelled`, reason `stale`; nothing is sent | none (`cancel_reason = stale`) | A summary with the count, first at once, then at most hourly |

**Only a flood wait, a reached limit and a closed window put a row back.** In all three, the message
certainly did not go out: for a flood wait Telegram refused it and said when to try again, even when
the wait came from the read mark or the typing, and for a limit or a window `send_text` was never called.
Every other failure ends the row: `send_text` is called exactly once for it, and
an unknown outcome is never retried by the sender, because the first attempt may have reached
Telegram and each new call gets a new `random_id`, so the contact would receive the message twice. A
cause of an uncertain row (a class name, or `SendDeadlinePassed`) goes to the log line; the row stores
`uncertain`. The notification carries the two internal ids only.

**Recipient versus content.** A recipient rejection will repeat for every message to this contact, so
the contact is paused and its queue cancelled. A contact the status rules cannot pause (it moved to
`paused` or `ignored` during the send) is logged and left as it is; the rest still happens. The row
is marked failed last, so a storage failure on the way leaves it `sending` for the stale check to
report. A content rejection says nothing about the contact, so only that row fails.

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
- **Stale check.** Every 60 s: `sending` rows claimed more than 195 s ago go the same way.
- **Cancellation mid-send** (shutdown) leaves the row in `sending`, for the next startup sweep.

The owner decides what to do after reading the chat. A step that fails (a database outage) is logged
with the class name, and the loop goes on: one bad row never stops it. Only cancellation does.

## The transport, the notifier and the adapter

- `MessageTransport` (`services/message_transport.py`) has exactly three methods, and only the sender
  calls them: `send_text(tg_id, text) -> SentMessage`, `show_typing(tg_id, seconds)` and
  `mark_read(tg_id)`.
- `OwnerNotifier` (`services/owner_notifier.py`) has exactly one method: `notify(text)`.
- `TelethonAdapter` (`userbot/adapter.py`) is the only module that touches the Telethon client
  directly and the only one with a Telethon write call: `send_message` with plain text, no parse
  mode and no link preview; `action` with `typing`, held for the given seconds through an injected
  sleep, cancellable, with `auto_cancel=False`, because the message clears the indicator and a cancel
  would be one more write; and `send_read_acknowledge` for the whole chat. It also implements the read Protocol `UserbotClient`, whose generic
  request call was replaced by `get_full_user(tg_id)`; only the adapter imports a request class
  (`GetFullUserRequest`). It logs nothing.
- `TelegramOwnerNotifier` (`bot/notifier.py`) sends one plain Russian message to the owner chat. The
  owner id comes from `Settings`, so no caller can address another chat. The texts are in
  `bot/messages.py` and reach the sender as templates (`SenderNotices`), because `services` cannot
  import `bot`. One attempt: a failure is logged with the class name and changes nothing.
- **The notifier deadline.** The sender wraps every owner notification in one place, `_notify`, in
  `OWNER_NOTIFY_DEADLINE` (15 s, `asyncio.timeout`). A timeout or an error of the notifier is logged
  with the class name only, and the sender goes on. The state decision is always made before the
  notice, so a bad notifier cannot change it. A hanging notifier delays the sequential loop by at most
  15 s per notice, and that notice is not sent again. Cancellation at shutdown is not swallowed.

## Two sources and the switch

| Source | Written by | Needs `AUTOPILOT_ENABLED` | Status check |
| --- | --- | --- | --- |
| `manual_test` | The owner's `outbox enqueue` command | No | Yes |
| `autopilot` | The reply job of the worker (CYR-17), [autopilot.md](autopilot.md) | Yes | Yes |

`AUTOPILOT_ENABLED` defaults to `false`. With it off the app enqueues no job, the job writes no row,
and an `autopilot` row left in the outbox is cancelled by the guard (`autopilot_disabled`). Switch it
on only after the checklist in [autopilot.md](autopilot.md#checklist-before-switching-it-on). It does
not switch the sender off: the sender always runs. The autopilot's own stop and cooldown pause
(`autopilot_state`) stop the generation, not the sending; the sender's state is a separate row.

## Owner commands

`python -m cyrano outbox ...` runs on the host like `make migrate`, with the app `Settings` and the
database up. The sending itself is done by the running app.

| Command | Makefile | What it does |
| --- | --- | --- |
| `outbox enqueue CONTACT_ID TEXT [--in-seconds N]` | `make outbox-enqueue CONTACT=... TEXT="..." [IN=...]` | Adds a `manual_test` row, due in N seconds (default 5, 0 to 86400). Prints the row id and the planned time |
| `outbox list [--state S] [--limit N]` | `make outbox-list [STATE=...] [LIMIT=...]` | Newest first (default 50, 1 to 1000): id, contact id, source, state, `send_at`, `due_at`, error class, `cancel_reason`. An empty value prints `-`. The reason is a stored constant such as `cancelled_by_owner`, `stale`, `superseded`, `send_rejected` or a guard refusal, so a stale cancel is told apart from an owner cancel. Never a text |
| `outbox cancel ID` | `make outbox-cancel ID=...` | Cancels a `scheduled` row. A row in any other state is refused with a message |
| `outbox retry ID [--in-seconds N] [--yes]` | `make outbox-retry ID=... [IN=...] [YES=1]` | Only for a `failed` row; any other state is refused with a message. Without `--yes` it prints a reminder to check the chat first and changes nothing. With `--yes` it sets `scheduled`, `send_at` and `due_at` to now + N seconds (default 5, 0 to 86400), clears `error_class` and `cancel_reason`, and prints the row id and the planned time |
| `outbox status` | `make outbox-status` | The sender state (running, paused with the time and reason, stopped with the reason), the six counters against their limits, and the rows per state (both sources; `autopilot status` counts the `autopilot` rows alone). Never a text, a name or a Telegram id |
| `outbox resume [--yes]` | `make outbox-resume [YES=1]` | Clears a pause, or with `--yes` also a stop, and says what was cleared. Nothing to clear is a harmless message and exit 0. Without `--yes` a stopped sender stays exactly as it is: it prints a reminder to check the account first and exits 1 |

The per-contact counters in `outbox status` show the contact with the most messages in that window
and its internal id, because the command takes no contact argument.

## After a notice

| Notice | What it means | What to do |
| --- | --- | --- |
| Flood pause | Telegram asked to wait; all sending waits and goes on by itself | Usually nothing. `outbox status` shows the time. `outbox resume` ends the pause early, which risks another flood wait |
| Sender stopped | The account is restricted or lost; nothing is sent until you act | Check the account in Telegram, then `outbox resume --yes`. If the session is gone (`AuthKeyUnregisteredError`, `AuthKeyInvalidError`, `AuthKeyPermEmptyError`, `AuthKeyDuplicatedError`, `SessionRevokedError`, `SessionExpiredError`, `SessionPasswordNeededError`, `ActiveUserRequiredError` or a bare `UnauthorizedError`), run `make login` and restart the app first. `FrozenMethodInvalidError` means Telegram froze the account: check it in Telegram, no new login helps. The reminder repeats every 6 h until you resume |
| Stop reminder | The sender is still stopped; sent every 6 h | The same as for the stop notice |
| Stale summary | Rows were cancelled as `stale` instead of being sent hours late; the notice gives the count and up to 5 contact ids | Nothing is needed. `outbox list --state cancelled` shows them; enqueue again only what you still want sent |
| Hourly or daily limit | Rows are postponed until a slot frees up; a row more than 6 h past its `due_at` is cancelled as `stale` when it comes back | Usually nothing. `outbox status` shows the counters. Raise the setting only if you mean it |
| Contact paused after a rejection | This contact cannot be written to; the contact's queue is cancelled | Nothing is needed. The contact is stored as `paused` with the reason `send_rejected` (CYR-21); the Resume button exists only on the manual-pause and the hand-over notices, so the contact stays paused until the owner decides, and a general Resume is CYR-26 |
| Contact paused by a manual message | You wrote to this contact by hand; the contact's scheduled rows are cancelled with the reason `manual_message` | Press «Возобновить автоответы» on the notice when you want automatic replies again. Nothing is sent before that |
| Contact handed over on a stop-trigger | The contact wrote about a topic the assistant never answers itself (see [stop-triggers.md](stop-triggers.md)); the contact is paused and its scheduled rows are cancelled with the reason `stop_trigger` | Answer the contact yourself, then press «Возобновить автоответы», «Оставить на паузе» or «Игнорировать» on the notice. Nothing is sent before that |

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
| Splitting a reply into several messages | CYR-36 |
