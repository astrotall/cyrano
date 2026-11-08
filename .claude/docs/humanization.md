# Humanization

Exists since CYR-18. Replies should look like they come from a person with a daily routine, and the
account should stay within safe behaviour. Two parts do this:

- **The planner** decides **when** a reply goes out. It is pure and writes nothing. Since CYR-17 the
  autopilot job calls it for every reply it writes, with the trigger message and the job's clock, and
  stores the result as the row's `send_at` and `due_at` ([autopilot.md](autopilot.md)); the owner's
  `schedule plan` command calls it too.
- **The pre-send part of the sender** decides **how** it goes out: read mark, a pause, typing, then
  the message. It runs for every row of the sender, the owner's test rows included.
- **The early read** (CYR-44) decides **when an incoming message is marked read** while a conversation is
  lively: soon after it arrives, not only when the reply starts ([below](#the-early-read)).

Since CYR-36 a reply the model wrote as several lines goes out as up to three messages, one per part,
with a short pause between them ([multi-part replies](outbox-and-sender.md#multi-part-replies)). The rule
is S6 in [safety-rules.md](safety-rules.md); the sender in [outbox-and-sender.md](outbox-and-sender.md).

## The profile

One JSON file, version 1, imported into PostgreSQL by a command, like the persona: the app never
reads a file for it. Nothing imported means the built-in default profile. Every unknown key is
refused. Errors give the JSON path and the rule, never a value and never the unknown key's name.

| Field | Default | Rules |
| --- | --- | --- |
| `version` | required | Exactly `1` |
| `timezone` | `TIMEZONE` from the settings | An IANA name. Left out or `null`, it follows the setting, also after a later change of the setting |
| `night.start` | `["01:00", "02:00"]` | `[lo, hi]`, `HH:MM`, `lo <= hi`, `hi` at most `05:00` |
| `night.end_weekday` | `["09:00", "09:00"]` | `[lo, hi]`, `lo` after `night.start`'s `hi`. Used for Monday to Friday dates |
| `night.end_weekend` | `["11:00", "12:00"]` | `[lo, hi]`, `lo` after `night.start`'s `hi`. Used for Saturday and Sunday dates |
| `busy` | `[]` | At most 20 windows `{"days": [...], "from": "HH:MM", "to": "HH:MM"}`; `days` is a non-empty list of `mon`..`sun` (repeats are harmless), `from` before `to`, same day |
| `reaction.ongoing_seconds` | `[1, 4]` | `[lo, hi]`, integers, 0 to 3600 |
| `reaction.new_seconds` | `[20, 120]` | `[lo, hi]`, integers, 0 to 3600 |
| `reaction.ongoing_window_minutes` | `10` | Integer, 1 to 120 |
| `read_pause_seconds` | `[1, 3]` | `[lo, hi]`, integers, 0 to 10 |
| `typing.chars_per_second` | `5` | Integer, 1 to 50 |
| `typing.min_seconds` | `2` | Integer, 0 to `max_seconds` |
| `typing.max_seconds` | `25` | Integer, `min_seconds` to 60 |
| `postponed_spread_minutes` | `[1, 15]` | `[lo, hi]`, integers, 0 to 120 |
| `split_pause_seconds` | `[3, 8]` | `[lo, hi]`, integers, 0 to 60 (`MAX_SPLIT_PAUSE_SECONDS`). The pause between two parts of one reply, counted from the actual send of the earlier part (CYR-36). A profile stored before the field existed reads with the default; no migration |
| `early_read_seconds` | `[0, 1]` | `[lo, hi]`, integers, 0 to 30 (`MAX_EARLY_READ_SECONDS`), `lo <= hi`; `[0, 0]` means strictly immediate. The delay of the early read mark, drawn uniformly (a float) from the range with the injected generator (CYR-44). A profile stored before the field existed reads with the default; no migration |

A sample with invented values: `examples/schedule.sample.json`. The worked examples below use it.

## The planner

`services/humanization.py`. The clock, the random generator and the profile are always passed in.
Everything goes in and out as aware UTC; the local rules are computed in the profile's zone.

**The night window of a date.** `night_window(date, profile)` gives one window per local calendar
date, and it lies inside that date: it never crosses midnight. The start comes from `night.start`,
the end from `night.end_weekday` or `night.end_weekend` by that date's weekday. Each boundary is drawn
at minute precision from a `random.Random` seeded with the date and a label, not from the injected
generator. So a date always has the same boundaries, across calls and restarts, while different dates
differ. Consequences that are easy to get wrong:

- 23:50 on Friday is free: it belongs to Friday's date, whose window ended that morning.
- Friday night after the start belongs to **Saturday's** window, so it ends at the weekend time.
- Sunday night after the start belongs to **Monday's** window, so it ends at the weekday time.

**Blocked.** `blocked(at, profile)` returns nothing, or the end and the kind (`night` or `busy`). A
moment is inside a window when `start <= t < end`: half-open, so the end itself is free. Busy windows
apply by the local weekday. Overlapping windows return the latest end, with the kind of that window.

**Ongoing.** `is_ongoing(burst_first_at, previous_activity_at, profile)` is true when there is
previous activity and the burst's first message came less than `ongoing_window_minutes` after it.
Exactly the window length is not ongoing. `burst_is_ongoing(history, trigger, profile)` applies it to a
history (the burst and the activity before it, see the burst below), and it is the one place both users
call: the planner for the reaction delay, and since CYR-43 the autopilot scheduler for the debounce,
through `HumanizationPlanner.conversation_ongoing(contact_id, message_id)`
([autopilot.md](autopilot.md#the-debounce)). An ongoing conversation gets the short debounce
(`AUTOPILOT_DEBOUNCE_ONGOING_SECONDS`, 4 s) and the short reaction; any other gets 12 s and the long
reaction. `tests/test_ongoing_rule.py` fails if a second copy of the rule appears.

**The send time.** `plan_send_time(now, trigger_at, ongoing, profile, rng)`:

1. `delay = uniform(ongoing_seconds)` when ongoing, else `uniform(new_seconds)`.
2. `candidate = max(now, trigger_at + delay)`: counted from the message, never in the past.
3. Up to 8 times: if the candidate is not blocked, stop. Otherwise
   `candidate = end + uniform(postponed_spread_minutes)` and remember the kind.
4. Eight pushes used up: the last candidate is taken without another check, and the reason says so.

| Reason | Meaning |
| --- | --- |
| `ongoing` | No push, the short delay |
| `delayed` | No push, the long delay |
| `postponed_night` | The last push left a night window |
| `postponed_busy` | The last push left a busy window |
| `exhausted` | Eight pushes were used; the time may still be blocked, and the sender's check catches that |

**Typing.** `typing_seconds(text, profile) = clamp(len(text) / chars_per_second, min_seconds, max_seconds)`.
For a reply in several parts it is computed for each part from that part's own text.

**The burst.** `HumanizationPlanner.plan(contact_id, trigger_message_id, now, *, ongoing=None)` (called by
the autopilot job, CYR-17) reads the last 20
messages of the contact. The burst is the run of consecutive incoming messages that ends with the
trigger; later messages are ignored. `burst_first_at` is the time of its first message, and
`previous_activity_at` the time of the message right before it, in either direction, or nothing. A
trigger that is not an incoming message within those 20 raises `PlanNotPossible`, which carries ids
only. When the burst fills all 20, there is no previous activity to see, so it counts as new. A
`PlanNotPossible` makes the autopilot job skip the message without a row.
`ongoing=True` or `False` replaces the decision from the history; only the autopilot job passes it, with
`True`, for a burst that waited the maximum wait and adopted its newest message
([autopilot.md](autopilot.md#the-maximum-wait)): that contact has already waited, so the reply gets the
short reaction.

`ScheduleService` gives the rest of the code `get_profile()` (stored or default, with the time zone
filled in) and `blocked_until(now)`.

### Worked examples

With the sample file: zone `Europe/Berlin`, night start 00:30 to 01:30, weekday end 08:00, weekend end
10:00 to 11:00, busy on Tuesday and Thursday from 09:00 to 10:30, default reactions and spread. All
dates are November 2026. "New" means no recent activity before the message.

| Message at | Kind | Window hit | Sent at | Reason |
| --- | --- | --- | --- | --- |
| Tue 15:00 | new | none | 15:00:20 to 15:02:00 | `delayed` |
| Tue 15:00 | ongoing | none | 15:00:01 to 15:00:04 | `ongoing` |
| Mon 00:15 | new | none, the start is 00:30 at the earliest | 00:15:20 to 00:17:00 | `delayed` |
| Mon 01:45 | new | Monday's night, ends 08:00 | 08:01 to 08:15 | `postponed_night` |
| Tue 03:00 | ongoing | Tuesday's night, ends 08:00 | 08:01 to 08:15 | `postponed_night` |
| Tue 09:30 | new | Tuesday's busy window, ends 10:30 | 10:31 to 10:45 | `postponed_busy` |
| Tue 08:59:45 | new | The delay lands in the busy window | 10:31 to 10:45 | `postponed_busy` |
| Wed 09:30 | new | none, no busy window on Wednesday | 09:30:20 to 09:32:00 | `delayed` |
| Fri 23:50 | new | none, Friday's window ended that morning | 23:50:20 to 23:52:00 | `delayed` |
| Sat 02:00 | new | Saturday's night, ends between 10:00 and 11:00 | Sat 10:01 to 11:15 | `postponed_night` |
| Sun 02:00 | new | Sunday's night, ends between 10:00 and 11:00 | Sun 10:01 to 11:15 | `postponed_night` |
| Sun 23:59:45 | new | none, Monday's window has not started | Mon 00:00:05 to 00:01:45 | `delayed` |

A message between 00:30 and 01:30 depends on the date: before that date's drawn start it is free,
after it it is blocked.

## The pre-send sequence

In the sender, per row: claim, stale check, guard, limiter, the availability check for autopilot rows
(first parts only), then, for every source:

1. `mark_read`, only for a single row or the first part of a reply.
2. A pause of `uniform(read_pause_seconds)`, only for a single row or the first part of a reply.
3. `show_typing(typing_seconds(text))`, for every part with its own text.
4. The guard again, on a freshly read contact.
5. `send_text`.
6. For a reply in several parts: the next part becomes due `uniform(split_pause_seconds)` after this
   send, and goes through the same sequence without steps 1 and 2.

A later part does not mark the chat as read again (it is read already and the contact sees no new
message from themselves), and the availability check does not stop a reply that has started: its parts
are seconds apart, so a window that begins between them does not cut the reply in half.

Reading comes before typing because that is the order a person does it in: the contact sees the
message read, then typing, then the reply. The other order shows someone answering a message they
have not opened. Details and the error table: [outbox-and-sender.md](outbox-and-sender.md).

**Cosmetic failures never block the send.** A failed read mark or typing is logged with the class name
and the send goes on, because the reply matters more than the indicator. Two failures are not cosmetic,
because they are about the account: `SendFloodWait` and `SendAccountBlocked`, handled exactly as on a
send.

## The early read

Without it, a message stays unread until the reply starts: the read mark is the first step of the
pre-send part, which comes after the debounce, the reaction delay and the claim. In a lively
conversation that is many seconds in which the contact sees their message unread while a person sitting
in the chat would have read it at once. Since CYR-44 the chat is marked read soon after the message
arrives, and only the read mark moves: typing and the pre-send sequence stay exactly where they are. The
read mark of the pre-send part still happens later and is harmless (a second read of a chat that is
already read shows nothing).

**It is a request, then an execution.** The intake registers a request, the sender's second coroutine
executes it. The registry (`services/early_read.py`) never touches the transport; only
`services/outbox_sender.py` calls `mark_read` ([outbox-and-sender.md](outbox-and-sender.md#the-early-read-coroutine)).

**Registered only when every condition holds**, each read fresh at that moment:

| Condition | Where it comes from |
| --- | --- |
| `EARLY_READ_ENABLED` is true | Setting, default on |
| `AUTOPILOT_ENABLED` is true | Setting; nothing happens with the autopilot off |
| The autopilot is neither stopped nor in a cooldown pause | `autopilot_state` |
| The sender is neither paused (flood wait) nor stopped (account problem) | `sender_state` |
| The contact exists and is `active` | A contact just handed over, paused or ignored is no longer active |
| The conversation is ongoing | The same rule as the adaptive debounce: `HumanizationPlanner.conversation_ongoing`, which calls `burst_is_ongoing` ([the ongoing rule](#the-planner)); there is no second copy |
| The moment is outside every blocked window | `ScheduleService.blocked_until(now)` is nothing: not in the night window, not in a busy window |

The intake asks the scheduler last: after the stop-trigger gate and after the autopilot scheduler, so a
contact that the gate handed over is no longer active and gets no request. Any failure of the scheduler (a
repository error, an unreadable profile, a message missing from the history) is caught, logged with the
class name only and never breaks the intake.

**The request.** One pending request per contact: a second request for a contact that already has one is
ignored and does not move its due time, so a burst of five messages gives one read mark. The due time is
the moment of the message plus `uniform(early_read_seconds)`. Between two early reads of one contact
there are at least 5 seconds (`EARLY_READ_MIN_INTERVAL_SECONDS`): a request registered or taken sooner
than that after the previous execution is deferred to the previous time plus the interval. The registry
is in memory only: a restart loses the pending requests, which is harmless because the reply path still
marks the chat read.

**Execution** (every 0.5 s, `EARLY_READ_POLL_INTERVAL`): take the due requests; check again everything
except the pace of the conversation (the settings, the autopilot, the sender, the contact, the blocked
windows), because the world may have changed in the few seconds since the message; call `mark_read` under
a 15 s deadline. A failed check skips the request. A flood wait pauses the whole sender with the usual rules
and notice and drops the request. An account-level error stops the sender. Any other failure is cosmetic:
logged with the class name, counted and dropped. The early read is not a message: no send limit counts it
and the outbox never sees it.

Examples with the default `[0, 1]` and a profile whose busy window is Tuesday 09:00 to 10:30:

| Situation | Result |
| --- | --- |
| Tue 15:00:00, the contact answered the owner at 14:59, a new message arrives | Read mark at 15:00:00 to 15:00:01 (the executor polls every 0.5 s, so it may land up to 0.5 s later) |
| Tue 15:00:00, the contact writes again at 15:00:02, after the first read mark was made at 15:00:00.4 | A new request is registered and deferred to the first execution plus 5 s, 15:00:05.4 |
| Tue 15:00:00, five messages within 0.2 s, before the executor polled | One request and one read mark; a message after the first read mark waits out the 5 s interval |
| Tue 15:00:00, the first message after two hours of silence | No request; the chat is marked read when the reply starts, as before |
| Tue 09:30, a message in an ongoing conversation | No request: inside the busy window |
| Tue 15:00:00, the message contains a meeting proposal | Handed over, contact paused, no request |
| Tue 15:00:00, the request is due, and at 15:00:00.2 the owner pauses the contact | Skipped at execution: not active |

## The safety net

The planner only proposes a time. Three things stand behind it, in this order at claim time:

- **The stale rule.** A row more than 6 h past its `due_at` is cancelled as `stale`, not sent late.
- **The status guard**, before the pre-send part and again right before the send.
- **The availability check.** An autopilot row due inside a window (the profile changed, a limit or a
  flood wait moved it there, or the plan was `exhausted`) goes back to `scheduled`, at the window's
  end plus the spread, with `error_class = outside_hours`. It is not dropped, it is not an attempt,
  `due_at` stays, and the owner gets no notice. `manual_test` rows skip this check: the owner asked
  for that message now. A later part of a reply skips it too.

**The stale rule still wins, on purpose.** `due_at` is untouched, so a row due at 02:30 and held until
the morning is more than 6 h old when it comes back, and is cancelled as `stale` with the usual summary
notice. A row the planner placed does not run into this, because its `due_at` is already after the
window. It takes a limit or a flood wait moving a row into the night, or a profile changed while rows
wait.

## The commands

`python -m cyrano schedule ...` runs on the host with the app `Settings` and the database up. They
print no text, no name and no Telegram id. Any error is one readable line and exit 1, a bad command
line included; an invalid configuration exits with 2 like every command.

| Command | Makefile | What it does |
| --- | --- | --- |
| `schedule import FILE` | `make schedule-import FILE=...` | Validates the whole file first: on any problem nothing is stored. Over 64 KiB, missing, not UTF-8 or invalid JSON are readable errors too. Then replaces the stored profile and prints a short confirmation |
| `schedule show` | `make schedule-show` | Says whether the stored or the default profile is in use, then prints the effective profile |
| `schedule check [--at "YYYY-MM-DD HH:MM"]` | `make schedule-check [AT="..."]` | Reads `--at` in the profile's zone, default now. Prints `free`, or the end and the kind. Writes nothing |
| `schedule plan CONTACT_ID` | `make schedule-plan CONTACT=...` | The planned local time, the reason and the seconds from now for the contact's latest incoming message. Reads, never writes |

The running app reads the stored profile on every row, so an import takes effect without a restart.

## Privacy

The profile describes the owner's real routine. The real file lives only at `data/schedule.json`:
`data/` is ignored by git and Docker, and `schedule*.json` is ignored anywhere in the repository, with
an exact exception for the sample. Never commit it, and never put it in an example. Logs carry the
busy window count on import and internal ids and the window kind on a hold, never the times of a
routine. A validation error never repeats a value from the file.
