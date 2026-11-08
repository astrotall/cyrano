# Proactive messages

Exist since CYR-23, behind the switch `PROACTIVE_ENABLED` (default `false`), and only while the autopilot
runs (`AUTOPILOT_ENABLED=true`, not stopped, not cooling down). When an `active` contact has been silent
for a long time after **our** last message, the assistant sometimes writes first: one short, natural
message that continues the conversation, inside strict limits, and it stops when the contact does not
answer. It reuses the whole reply pipeline: the worker, the reply generator, the planner, the outbox
chain, the one sender and every safety check. Rules S1, S2, S3, S7: [safety-rules.md](safety-rules.md).
Reasons: [decisions.md](decisions.md).

## What it does and does not do

- It writes only to an `active` contact whose last message (by `(created_at, id)`) is outgoing and who
  has written at least one message. It **never opens a conversation**: a history without an incoming
  message is never eligible.
- It does not revive a conversation that has been silent for too long: a silence longer than
  `PROACTIVE_MAX_SILENCE_DAYS` (default 3 days) is left alone, so switching the feature on does not send one
  message to every contact who went quiet weeks ago, which would look needy and out of place (CYR-53).
- It writes at most `PROACTIVE_MAX_PER_SILENCE` messages (default 2) after the contact's last incoming
  message, at least `PROACTIVE_MIN_INTERVAL_HOURS` (default 24) apart, and at most
  `PROACTIVE_DAILY_LIMIT` (default 20) a day over all contacts.
- It writes only inside the profile's `proactive_window` (default 10:00 to 22:00 local time) and outside
  every night and busy window, and never within the profile's `wrapup_soft_minutes` before one of them
  (since the wrap-up part of CYR-23).
- It does not pause a contact who stays silent: after the maximum it simply writes nothing more until the
  contact writes again ([decisions.md](decisions.md)).
- It has no morning greeting, no per-contact switch and no use of the contact's last seen (open
  questions).

## Settings and defaults

All optional; a wrong value is a readable `ConfigError` that names the variable and never echoes the
value. None of them is in the startup log line.

| Name | Default | Range | Meaning |
| --- | --- | --- | --- |
| `PROACTIVE_ENABLED` | `false` | | The switch, read by the app's sweeper, the worker's job and the sender's guard |
| `PROACTIVE_AFTER_HOURS` | 6 | 1 to 72 | The silence after our last message (or after the last resume) that makes a contact eligible |
| `PROACTIVE_MAX_SILENCE_DAYS` | 3 | 1 to 30 | The longest silence, in days, after which a contact is still eligible; a longer silence is left alone. Counted in hours it must be at least `PROACTIVE_AFTER_HOURS`, otherwise the `ConfigError` names both variables |
| `PROACTIVE_MAX_PER_SILENCE` | 2 | 1 to 5 | Proactive messages after the contact's last incoming message |
| `PROACTIVE_MIN_INTERVAL_HOURS` | 24 | 1 to 168 | The least time between two proactive messages to one contact |
| `PROACTIVE_DAILY_LIMIT` | 20 | 1 to 200 | Proactive messages in any 24 hours over all contacts |
| `proactive_window` (schedule profile) | `["10:00", "22:00"]` | `HH:MM`, start before end on one day | When a proactive message may go out ([humanization.md](humanization.md#the-profile)) |

Constants (`domain/proactive.py`): `HOURS_PER_DAY` (24, converts the days of the upper bound),
`PROACTIVE_SWEEP_LIMIT` (10 contacts per scan),
`MODEL_SKIP_POSTPONE_SECONDS` (12 h), `FAILURE_POSTPONE_SECONDS` (1 h), `PROACTIVE_DAILY_WINDOW_SECONDS`
(24 h), the job name `proactive_to_contact` and the job id `proactive-<contact id>-<last message id>`.

## The eligibility rule

`services/proactive_eligibility.py`. The rule exists once: the pure function `decide_proactive(snapshot,
rules)` over a `ProactiveSnapshot`, and the thin `ProactiveEligibility.check(contact_id)`, which reads the
snapshot fresh (autopilot state, sender state, contact, the dialogue tail, the row counts) and calls it.
The sweeper and the job both call `check`; `tests/test_proactive_structure.py` fails if a second copy of
the rule appears. The conditions are checked in this order, and the first that fails is the reason
(`ProactiveReason`):

1. `PROACTIVE_ENABLED` is true, otherwise `proactive_disabled`.
2. `AUTOPILOT_ENABLED` is true, otherwise `autopilot_disabled`.
3. The autopilot is not stopped (`autopilot_stopped`) and not in a cooldown pause (`autopilot_paused`).
4. The sender is not stopped (`sender_stopped`) and not paused by a flood wait (`sender_paused`).
5. The owner is not about to become unavailable: `ScheduleService.unavailable_soon(now)` gives no
   wrap-up context, in either phase ([humanization.md](humanization.md#wrapping-up-before-a-window)),
   otherwise `unavailable_soon`. A proactive message never starts shortly before a night or busy window;
   this does not depend on `WRAPUP_ENABLED`.
6. The contact exists (`contact_missing`) and is `active` (`contact_not_active`).
7. `contacts.proactive_not_before` is empty or not in the future, otherwise `postponed`.
8. The history holds at least one incoming message, otherwise `no_incoming`.
9. The last message of the history is outgoing, otherwise `last_message_incoming`.
10. The silence, counted from the **later** of that last message's time and `contacts.reply_after`, is at
   least `PROACTIVE_AFTER_HOURS`; exactly the threshold is enough. Otherwise `silence_too_short`.
11. The same silence, measured the same way, is at most `PROACTIVE_MAX_SILENCE_DAYS` days; exactly the
    bound is still enough, only a strictly longer silence fails. Otherwise `silence_too_long` (CYR-53).
12. Fewer than `PROACTIVE_MAX_PER_SILENCE` proactive messages belong to this silence, otherwise
    `silence_limit_reached`.
13. The contact's newest proactive message is at least `PROACTIVE_MIN_INTERVAL_HOURS` old (exactly the
    interval is enough), otherwise `min_interval`.
14. Fewer than `PROACTIVE_DAILY_LIMIT` proactive messages over all contacts fall in the last 24 hours,
    otherwise `daily_limit`.

All conditions true gives `eligible`.

**What counts as a proactive message.** One message is one **first part** of a `proactive` chain
(`follows_id IS NULL`), in any state except `cancelled`, so a two-line message counts once and a message
that was superseded or cancelled counts not at all. A message belongs to the current silence when its
trigger message (our last message when the job ran) is newer than the contact's last incoming message by
`(created_at, id)`. Its time for the interval and the daily cap is its `due_at`, the planned send time
that the job sets from its own clock; a message that is planned but not sent yet is therefore counted
too, and the rule never compares the database clock with the job's clock.

**The repository** (`ProactiveRepository` in `services/repositories.py`, `SqlProactiveRepository` in
`db/repositories.py`): `dialogue_tail` (the last message and the last incoming message),
`count_proactive_after(contact_id, message_id)`, `newest_proactive_due_at(contact_id)`,
`count_proactive_due_since(since)`, `contacts_due_for_proactive(...)` and
`set_proactive_not_before(contact_id, when)`. The scan returns `active` contacts whose postponement has
passed, whose last message is outgoing, who have an incoming message and whose silence reached the
threshold (`silent_since`) without exceeding the upper bound (`silent_not_before`, inclusive, counted from
the same silence start as the pure rule); it also leaves out contacts at the per-silence maximum and inside the interval, so contacts
that can never be eligible do not fill the 10 places of a scan. The most recent silence comes first.
The pure rule re-checks everything. A contact whose silence is too long is simply left alone: the scan
never returns it, the job that meets it anyway skips with `silence_too_long`, and nothing is written to
its row (`proactive_not_before` stays as it was), no outbox row is made and nothing is logged per contact.

## The flow

```
app: ProactiveSweeper (every 60 s, both switches on)
     |  contacts_due_for_proactive (at most 10) -> ProactiveEligibility.check (the one rule)
     v
Redis: Arq job "proactive_to_contact", id proactive-<contact id>-<last message id>, no delay
     |
     v
worker: ProactiveJobRunner.run(contact_id, last_message_id)
     |  1 the rule again; another last message -> superseded; a row for it -> row_exists
     |  2 ReplyGenerator.generate(purpose=proactive) through the shared GenerationPolicy
     |  3 SKIP -> postpone 12 h; NEEDS_OWNER -> hand-over; failures as in the reply job
     |  4 plan_proactive_time (window, night, busy)
     |  5 split_reply -> OutboxService.enqueue_sequence(source proactive, trigger = last message)
     |  6 proactive_not_before = now + PROACTIVE_MIN_INTERVAL_HOURS
     v
app: OutboxSender (guard, stale, limits, availability, window, supersession, read, typing, send)
```

**The sweeper** (`services/proactive_sweeper.py`) is a background task of the app, started with the
autopilot and summary sweepers and cancelled first on shutdown, with an injected clock and sleep. It
sweeps once at start and then every `SWEEP_INTERVAL_SECONDS` (60). With either switch off it reads
nothing. Each eligible contact gets one job through `ProactiveJobQueue.enqueue(contact_id,
last_message_id)` (`ArqProactiveJobQueue` in `worker/queue.py`, the shared Redis pool). The job id holds
the last message, so a waiting job is not doubled. A failing scan or queue is logged with the class name
and never stops the loop.

**The job** (`services/proactive_job.py`, `ProactiveJobRunner.run(contact_id, last_message_id, *,
attempt)`; no Arq import) returns a `ProactiveJobResult` (outcome, reason, detail, row id, parts) or
raises `RetryJob`. The worker function `proactive_to_contact` (`worker/jobs.py`) validates the arguments
(`ProactiveJobRequest`) and turns `RetryJob` into `arq.Retry`. Its steps:

1. The rule again, on fresh data; a failed condition is a skip with its reason. The snapshot's last
   message is not `last_message_id`: skip, `superseded`. A row for `(contact, last_message_id)` exists:
   skip, `row_exists`, before any model call.
2. `ReplyGenerator.generate(contact_id, purpose=proactive)` through `GenerationPolicy`, the helper the
   reply job uses ([autopilot.md](autopilot.md#the-job)), with the same retry and hand-over rules.
3. The result: see [the outcomes](#skip-and-needs_owner) and [the failure policy](#the-failure-policy).
4. `plan_proactive_time(now, profile, rng)` gives the send time
   ([humanization.md](humanization.md#proactive-messages)).
5. `split_reply` and `OutboxService.enqueue_sequence(contact_id, parts, send_at, proactive,
   last_message_id)`: one chain, the guard once. The unique index on `(contact_id, trigger_message_id)`
   makes the job idempotent. `EnqueueRefused` is a skip with the refusal as the detail.
6. `proactive_not_before = now + PROACTIVE_MIN_INTERVAL_HOURS`.

One INFO line `proactive job finished` per run (logger `cyrano.services.proactive_job`): `contact_id`,
`last_message_id`, `attempt`, `outcome`, `reason`, `detail`, `row_id`, `parts`; `proactive job will be
retried` with the error class and the delay instead when it is retried. Never a text, a name, a username
or a Telegram id.

| Outcome | Reasons |
| --- | --- |
| `queued` | `queued`; the detail is the planner's reason (`delayed`, `postponed_window`, `postponed_night`, `postponed_busy`, `exhausted`) |
| `skipped` | every reason of the rule, `superseded`, `row_exists`, `model_skip`, `no_messages`, `contact_missing`, `enqueue_refused`, `provider_unavailable` (the cooldown could not be stored) |
| `handed_over` | `needs_owner`, `truncated`, `reply_invalid`, `prompt_too_large`, `mode_unavailable` |
| `stopped` | `persona_missing`, `provider_auth`, `provider_request` |
| `paused` | `provider_unavailable` |

## SKIP and NEEDS_OWNER

The proactive prompt ([reply-generation.md](reply-generation.md#the-proactive-purpose)) tells the model to
answer with exactly `SKIP` when the conversation ended naturally (a goodbye, good night, the contact is
busy) or when it cannot continue without inventing facts about the owner. The `NEEDS_OWNER` rule stays.

| The model answers | What happens |
| --- | --- |
| `SKIP` (exact after trimming quotes, backticks and punctuation, any case, or as a word inside a longer reply) | No row; `proactive_not_before = now + 12 h`; skip `model_skip`. Nothing is sent, the owner gets no notice. (A reply of the autopilot has its own `model_skip` since CYR-23, inside the wrap-up time only: [autopilot.md](autopilot.md#the-job)) |
| `NEEDS_OWNER` (wins over `SKIP`) | `HandOverService.hand_over` with the source `model` and no classes: the contact is paused, its scheduled rows are cancelled and the owner gets the hand-over notice |

## The window and the planner

The job plans the time with `plan_proactive_time` (inside the window and outside the night and busy
windows, [humanization.md](humanization.md#proactive-messages)). The sender checks the window again as a
safety net: a due `proactive` first part outside `proactive_window` goes back to `scheduled` at the next
window start plus `uniform(postponed_spread_minutes)`, with `error_class = outside_window`; it is not an
attempt and `due_at` stays, so the stale rule still counts from the planned time. A row inside a night or
busy window is held like an `autopilot` row (`outside_hours`).

## Supersession

A `proactive` first part that has not started is cancelled as `superseded` by the sender when **any**
message newer than its trigger exists, incoming (the contact wrote) or outgoing (the owner or another
reply), by `(created_at, id)`; the check runs once, after the availability check and before the read
mark, exactly as for `autopilot` rows. Once the read mark has begun the message is finished, and a later
part is never superseded. The manual-message detector and a hand-over cancel the contact's scheduled
rows, proactive ones included. Details:
[outbox-and-sender.md](outbox-and-sender.md#the-supersession-check).

## The failure policy

The same as the reply job's, through the same `GenerationPolicy` and the same autopilot-state operations
([autopilot.md](autopilot.md#the-failure-policy)), plus the postponement:

| Failure | What happens | `proactive_not_before` |
| --- | --- | --- |
| A truncated reply | One more generation with twice the tokens; truncated again: hand-over | unchanged |
| `ReplyInvalid` | One more generation; invalid again: hand-over | unchanged |
| `PromptTooLarge`, `DefaultModeUnavailable`, `NEEDS_OWNER` | Hand-over (model) | unchanged |
| `PersonaMissing`, `LLMAuthError`, `LLMRequestError` | The autopilot stops, one notice per new stop | now + 1 h |
| `LLMRateLimitError`, `LLMUnavailableError`, `LLMInvalidResponseError`, any unexpected exception | `RetryJob` after 60, 300, 900 s; after the 4th try the autopilot pauses for 15 min, one notice per new pause | now + 1 h after the last try only; a retry does not postpone, so the retried job is still eligible |

## The commands

| Command | What it shows |
| --- | --- |
| `reply-preview CONTACT_ID --proactive` (`make reply-preview CONTACT=... PROACTIVE=1`) | The proactive message the model would write now, as parts, or `skip (model signal)`, with the usual gender and stats lines. Writes nothing and sends nothing |
| `autopilot status` | Ends with `switch: PROACTIVE_ENABLED=...` and `proactive rows: ...` (the `proactive` rows per state) |
| `outbox list` | `source=proactive` for these rows |
| `schedule show` | The `proactive window` line |

## Checklist for turning it on

1. The autopilot works: replies, timing, a stop-trigger, a manual message
   ([autopilot.md](autopilot.md#checklist-before-switching-it-on)).
2. Summaries work (`SUMMARY_ENABLED=true`, `make summary-show CONTACT=...` gives a sensible memory): the
   follow-up asks about something the contact mentioned.
3. The schedule holds a `proactive_window` you are happy with (`make schedule-show`).
4. `make reply-preview CONTACT=... PROACTIVE=1` gives messages you would send, and `skip` after a goodbye.
5. Set `PROACTIVE_ENABLED=true` in `.env`, restart the app and the worker, and watch one test contact of
   your own: the follow-up after the silence, its time, the stop after the maximum, a reply of the contact
   cancelling a waiting follow-up, `make autopilot-status`.

To switch it off, set `PROACTIVE_ENABLED=false` and restart both. Rows already written are cancelled by
the sender's guard (`proactive_disabled`).

## Privacy

The prompt holds what the reply prompt holds ([reply-generation.md](reply-generation.md#what-goes-to-the-provider)).
Logs, notices and command output carry internal ids, counts, reasons and class names only ([S9](safety-rules.md)).
