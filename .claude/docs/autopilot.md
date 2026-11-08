# Autopilot

Exists since CYR-17, behind the switch `AUTOPILOT_ENABLED` (default `false`). The autopilot turns the
incoming messages of an `active` contact into ONE generated reply per burst and puts it in the outbox.
It never sends: the Telethon session lives in the app process, so the one sender there sends the row
([outbox-and-sender.md](outbox-and-sender.md)). The Arq worker generates and writes rows; it never
imports Telethon. Rules S1, S2, S3: [safety-rules.md](safety-rules.md). Reasons:
[decisions.md](decisions.md).

## The flow

```
contact writes
     |
     v
app: IntakeService ----> stop-trigger gate (CYR-20): a hit pauses the contact, nothing is enqueued
     |
     v
app: AutopilotScheduler.on_incoming ---- switch on, autopilot running, contact active (fresh read)
     |                                    |
     |   app: AutopilotSweeper (every 60 s) repairs lost jobs, restarts, a Redis flush,
     |        and messages that arrived while the contact was pending
     v                                    v
Redis: Arq job "reply_to_contact", id reply-<contact id>-<message id>, deferred by the debounce
       (4 s in an ongoing conversation, 12 s otherwise, CYR-43)
     |
     v
worker: ReplyJobRunner.run ---- skips, hand-over, stop or cooldown pause (tables below)
     |
     v
HumanizationPlanner.plan (send time) ----> split_reply ----> OutboxService.enqueue_sequence
                                                 (guard once, every part's text, one chain of 'autopilot' rows)
                                                 |
                                                 v
                                app: OutboxSender (guard again, limits, window, read, typing, send)
```

## Where the code lives

| File | What it holds |
| --- | --- |
| `domain/autopilot.py` | Every constant below, `AutopilotState`, `ReplyJobRequest` (the job arguments), `ReplyJobResult`, `reply_job_id`, `retry_delay` |
| `domain/enums.py` | `AutopilotStopReason`, `AutopilotPauseReason`, `ReplyJobOutcome`, `ReplyJobReason` |
| `domain/errors.py` | `RetryJob(defer_seconds, error_class)`, `ReplyAfterRefused`, `AutopilotStateMissing` |
| `services/reply_job.py` | `ReplyJobRunner`, `AutopilotNotices`, `burst_messages`, `burst_texts`. No Arq import |
| `services/reply_split.py` | `split_reply`: the pure split of a reply into at most `MAX_REPLY_PARTS` parts (CYR-36) |
| `services/autopilot.py` | `AutopilotScheduler` (the debounce is chosen with `HumanizationPlanner.conversation_ongoing`, CYR-43), `AutopilotSweeper`, `autopilot_running` |
| `services/reply_job_queue.py` | The one-method `ReplyJobQueue` Protocol |
| `services/repositories.py` | `AutopilotStateRepository` (`get`, `stop`, `pause_until`, `resume`) and `ReplyBacklogRepository` (`awaiting_reply`, `count_unanswered_active`) |
| `db/repositories.py`, migration `0009` | `SqlAutopilotStateRepository`, `SqlReplyBacklogRepository`, `contacts.reply_after`, the `autopilot_state` table |
| `worker/queue.py` | `ArqReplyJobQueue`, the queue the app uses |
| `worker/jobs.py` | `reply_to_contact`, the Arq job function |
| `worker/composition.py` | `build_worker_resources`: everything the job needs, and how it is closed |
| `worker/settings.py` | `WorkerSettings`, `startup`, `shutdown` |
| `bot/app.py`, `bot/notifier.py`, `bot/messages.py` | `NoticeBot` (send-only), `AUTOPILOT_NOTICES`, the Russian texts |
| `autopilot_cli.py` | `autopilot status` and `autopilot resume` |

Only the `worker` package imports `arq`. The app imports `cyrano.worker.queue` and nothing else of the
worker; the worker imports neither Telethon nor `userbot`. `tests/test_one_sender.py` pins all three.

## The debounce

Every new incoming message of an `active` contact enqueues one job. The job id is
`reply-<contact id>-<message id>`, so the same message is never queued twice while its job exists. A
burst therefore makes one job per message, and a job that finds a newer incoming message when it runs
skips (`superseded`), unless the burst has waited too long ([the maximum wait](#the-maximum-wait)).
Only one job of a burst generates, and the whole burst is in its prompt.

**The adaptive debounce (CYR-43).** `AutopilotScheduler.on_incoming` defers the job by
`AUTOPILOT_DEBOUNCE_ONGOING_SECONDS` (default 4) when the conversation is ongoing and by
`AUTOPILOT_DEBOUNCE_SECONDS` (default 12) otherwise. "Ongoing" is the planner's rule, in one place
(`HumanizationPlanner.conversation_ongoing`, which calls `burst_is_ongoing` and `is_ongoing` in
`services/humanization.py`): the burst is the run of consecutive incoming messages that ends with the new
message, and the conversation is ongoing when the activity right before the burst's first message, in
either direction, lies less than the profile's `ongoing_window_minutes` (10) before it. Exactly the window
length is not ongoing; a first message ever is never ongoing. The scheduler reads the last 20 messages and
the schedule profile for it. If that fails (an unreadable profile, a database error, a message not found
among the last 20), the job is deferred by the normal debounce and one WARNING
`the conversation pace was not decided` with the class name is logged; the job is enqueued anyway.
`tests/test_ongoing_rule.py` fails if a second copy of the rule appears.

Why: in a conversation where both sides are writing, a contact who sends a message every 10 to 20 s
never left a 12 s gap, so every job found a newer message and skipped, and nothing was sent (seen live).
The short debounce is shorter than such a gap. A contact who comes back after a pause still gets the
longer one, so a burst of short first messages is still collected into one reply.

A job can only skip itself when the newer message already exists at the time it runs. The reaction
delay of the planner (20 to 120 s from the trigger's time for a new conversation) is longer than the
debounce window, so the job of an earlier message may already have written its row when the next
message arrives. That row is handled by [one reply per burst](#one-reply-per-burst).

## The maximum wait

CYR-43. A burst whose gaps are all shorter than the debounce would otherwise skip job after job for as
long as the contact keeps writing (starvation). The job therefore measures the **burst age**: now minus
the time (`created_at`, Telegram's send time) of the first message of the current unanswered run, which
is the run of consecutive incoming messages that ends with the newest message `N` of the contact, after
the last outgoing message and after `reply_after`, among the last `BURST_HISTORY_MESSAGES` (20) messages.
It is the same run as the burst of the stop-trigger check.

| The newest message `N` | Burst age | What the job does |
| --- | --- | --- |
| is the trigger | any | Goes on as before |
| is outgoing (newer than the trigger) | any | Skips, `answered` |
| is a newer incoming message | below `AUTOPILOT_MAX_BURST_WAIT_SECONDS` (default 30) | Skips, `superseded`; a later job handles the burst |
| is a newer incoming message | at or above `AUTOPILOT_MAX_BURST_WAIT_SECONDS` | Does not skip: adopts `N` as the **effective trigger** |

The boundary is pinned: the maximum wait is reached when the burst age is greater than or equal to it.
With the effective trigger, everything that follows in this run uses `N`: the existing-row check, the
stop-trigger check on the burst up to `N`, the generation (whose history ends with `N`), the planner, the
job-time cancellation of older rows and the enqueue, so the row carries `trigger_message_id = N`. The
sender's first supersession check therefore finds no message newer than the row's trigger and does not
cancel it. Several jobs that adopt the same `N` write one row: the existing-row check and the partial
unique index on (contact, trigger) keep a second one out, and a second enqueue returns the stored chain.
The job of `N` itself later finds that row and skips (`row_exists`).

An adopted reply is planned with the **short reaction** (`reaction.ongoing_seconds`, 1 to 4 s), whatever
the conversation looked like before the burst: the contact has already waited the maximum wait. With the
long reaction of a new conversation (20 to 120 s) the next message would arrive first and the sender would
cancel the row before it started, so a fresh conversation with a fast writer would still starve. This is
the only place where the planner's own ongoing decision is overridden (`plan(..., ongoing=True)`).

The result and the `reply job finished` line carry the detail `adopted_newest` (`ADOPTED_NEWEST_DETAIL`):
for a queued reply instead of the planner's reason, and for any other outcome of an adopted run that has
no detail of its own (`row_exists`, for example). A hand-over, a refused enqueue, a stop or a pause keep
their own detail. The outcome and reason tables below are unchanged; `trigger_message_id` in the log line
stays the job's own trigger, and the row shows `N`.

### A lively conversation

The scenario of `tests/services/test_autopilot_lively.py`, with the defaults (debounces 12 and 4 s,
maximum wait 30 s, the default profile without a night window, the default send limits, a one-line reply
of 44 characters, so 8.8 s of typing), seeded and invented: a contact with no earlier messages writes
every 10 s from 0 to 170 s (18 messages) and then stops.

| Time (s) | What happens |
| --- | --- |
| 0, 10, 20 | No activity before the burst: each job is deferred 12 s |
| 12, 22 | The jobs find a newer message; the burst is 12 and 22 s old: `superseded` |
| 32 | The burst is 32 s old: the job adopts the message of 30 s, plans the short reaction and writes row 1 |
| 32 to 42.6 | The sender claims row 1, marks the chat read, pauses, types; the message of 40 s arrives during the typing and cancels nothing. **Reply 1 at 42.6** |
| 42 | The job of the 30 s message adopts the 40 s message and writes row 2; the sender cancels it before its read mark, because reply 1 is newer than its trigger |
| 50 | After reply 1 the conversation is ongoing: 4 s debounce. The job at 54 answers the 50 s message. **Reply 2 at 66.1** (the 60 s message arrived during the typing) |
| 60 to 100 | Each message gets its own job and row; the per-contact minute limit (2) holds each row until 102.6, and each new job cancels the waiting older row (none has started) |
| 104 | The row for the 100 s message is claimed when the slot frees. **Reply 3 at 114.4** |
| 120 to 136 | **Reply 4 at 136.4**, for the 120 s message |
| 140 to 174 | Rows held by the limit until 174.4, older ones cancelled by newer jobs |
| 174 to 184.5 | The row for the last message (170 s) is sent. **Reply 5 at 184.5**; nothing more follows |

The first reply comes 42.6 s after the first message; no reply gap is longer than 48.3 s, below the
maximum wait plus the profile's longest typing (30 + 25 = 55 s), which the test asserts for every gap and
for every message of the contact. In the steady state the pace is set by `SEND_LIMIT_CONTACT_MINUTE`, not
by the debounce: two replies per minute at most. The test also asserts that no trigger has a second row,
that every row whose typing began was sent, that every generated reply saw every message of the contact
since the previous reply up to its effective trigger (within the 20 messages of the history), and an
upper bound on the number of replies: at most one per incoming message and at most
`SEND_LIMIT_CONTACT_MINUTE` in every started minute of the scenario (2 x 6 = 12 for its 350 s); it sent 5.
The cost is the generation: 14 replies were generated for 5 sent, because a row held by the limit is
superseded by the next message's row before it can start. A second scenario (three messages at 0, 20 and
40 s, then silence) ends with exactly one reply, for the last message, whose prompt holds all three.

## One reply per burst

CYR-41, narrowed by CYR-43. A contact that writes three messages within a minute gets ONE reply, written
for the last one. A row of source `autopilot` that has **not started** is cancelled with the reason
`superseded` (no owner notice) in two places:

1. **In the job**, after the planner gave a send time and before `OutboxService.enqueue_sequence`: the
   contact's other `autopilot` replies whose first part is still `scheduled` and whose trigger message is
   older than the new (effective) trigger, by `(created_at, id)`, are cancelled in one statement, every
   waiting part of each such reply included (`OutboxRepository.cancel_superseded_autopilot_rows`). A reply
   whose first part is `sending` or `sent` is never touched, so its later parts are sent (CYR-43; under
   CYR-36 its waiting parts were cancelled). `manual_test` rows, rows of other contacts and rows without a
   trigger are never touched. The idempotent skip when a row for the new trigger exists (step 5) is
   unchanged.
2. **In the sender**, for a first part (or a single row) it has just claimed: once, after the availability
   check and before `mark_read` ([the supersession check](outbox-and-sender.md#the-supersession-check)).
   A newer message, incoming or outgoing, than the row's trigger cancels it. Once the read mark is under
   way the reply is finished: a newer message no longer cancels it before `send_text`, and a later part of
   a reply is never superseded (CYR-43).

A burst end to end, with an invented timeline: messages at 0, 40 and 80 s, jobs at 12, 52 and 92 s.
The job at 12 s writes row 1 for 48 s (the planner drew a delay of 48 s). The second message arrives at
40 s, so when the sender claims row 1 at 48 s it finds a newer message and cancels it. The job at 52 s
writes row 2 for 84 s. The third message arrives at 80 s: the sender cancels row 2 at 84 s. The job at
92 s writes row 3 for 193 s; no newer message arrives, and it is sent, the only message the contact
receives. Had a job run while an older row was still waiting, it would have cancelled that row itself.
Every skip or cancel in this story writes no notice and counts no attempt.

An outgoing message newer than the trigger also supersedes a row that has not started. The detector of
a manual message cancels the contact's scheduled rows as well (`manual_message`), later parts included; a
row that was already claimed is stopped by the sender's second guard check, because the contact is
`paused` by then.

**The trade-off (CYR-43).** A reply whose read mark has begun goes out even when the contact wrote again
during the read pause or the typing, so in a fast chat a message may get its own short reply and the next
reply answers what came after it. Cancelling there made a fast writer starve (seen live: the next message
cancelled the reply in its typing phase, and the second part of a reply whose first part had been sent).

"Newer" means newer by the pair `(created_at, id)` and `created_at` is Telegram's send time for both
directions ([Message times](contact-lifecycle.md#message-times)). A message that a catch-up stores late
but that was written earlier is older than the trigger and never supersedes it, and a message written
many hours ago is old by its own time however recently it was stored.

## The job

`ReplyJobRunner.run(contact_id, trigger_message_id, *, attempt)` returns a `ReplyJobResult` (outcome,
reason, an optional detail, the row id of the first part and the number of parts) or raises `RetryJob(defer_seconds)` for a transient failure.
`attempt` is Arq's `job_try`. Every check reads fresh data; nothing is cached between jobs.

1. The switch is off, the autopilot is stopped, or a cooldown pause lasts beyond now: skip.
2. The contact is missing or not `active`: skip.
3. The trigger message is missing (or belongs to another contact), is not incoming, has a time at or
   before `contacts.reply_after`, or is older than the stale age by its Telegram time: skip.
4. Superseded: the latest message of the contact is not the trigger. A newer outgoing one (the owner or
   the sender answered) gives `answered`. A newer incoming one gives `superseded` while the burst is
   younger than `AUTOPILOT_MAX_BURST_WAIT_SECONDS`; at or past it, the newest message becomes the
   effective trigger for steps 5 to 10 ([the maximum wait](#the-maximum-wait)).
5. An outbox row already exists for (contact, effective trigger), in any state: skip.
6. The stop-trigger detector runs on the burst: the run of consecutive incoming messages that ends
   with the effective trigger, only messages after `reply_after`. A hit calls
   `HandOverService.hand_over` with the classes and the source `text`.
7. `ReplyGenerator.generate` with the contact's mode; the generator adds the stored summary itself
   (CYR-25, [summaries.md](summaries.md)):
   - `needs_owner`: hand-over with the source `model` and no classes;
   - `truncated`: one more generation with `max_tokens` = `LLM_REPLY_MAX_TOKENS` x 2, capped at
     `TRUNCATION_MAX_TOKENS`; truncated again: hand-over (model);
   - `ReplyInvalid`: one more generation; invalid again: hand-over (model);
   - `PromptTooLarge` and `DefaultModeUnavailable`: hand-over (model);
   - `NoMessagesToReplyTo`: skip;
   - `PersonaMissing`: stop the autopilot (`persona_missing`), one notice if newly stopped.
8. LLM errors: `LLMAuthError` stops with `provider_auth`, `LLMRequestError` (402 insufficient balance
   included) with `provider_request`, one notice if newly stopped, never retried.
   `LLMRateLimitError`, `LLMUnavailableError`, `LLMInvalidResponseError` and any unexpected exception
   raise `RetryJob` with the next delay; on the last try the autopilot pauses for the cooldown
   (`provider_unavailable`), one notice per new pause, and no row is written.
9. `HumanizationPlanner.plan(contact_id, trigger_message_id, now)` with the effective trigger, and with
   `ongoing=True` for an adopted one; `PlanNotPossible` is a skip.
10. Before the enqueue, the contact's older `autopilot` replies that have not started are cancelled as `superseded`
    ([One reply per burst](#one-reply-per-burst)). Then the reply is split (`split_reply`, CYR-36): at
    every run of line breaks, each part stripped, empty parts dropped, the order kept, at most
    `MAX_REPLY_PARTS` (3) parts, and any further lines merged into the last part with one space. Then
    `OutboxService.enqueue_sequence(contact_id, parts, send_at, autopilot, trigger_message_id)` stores one
    chain ([multi-part replies](outbox-and-sender.md#multi-part-replies)): every part validated, the guard
    once, one transaction. An `EnqueueRefused` (one bad part refuses the whole reply) is a skip with its
    reason as the detail. A duplicate enqueue returns the stored chain and creates no part.

Everything is idempotent: a job run twice, a retried job, a job that races the sweeper's job for the
same message, or several jobs that adopt the same newest message write one row at most (step 5 and the
partial unique index of the outbox).

### Outcomes and reasons

| Outcome | Reason | When |
| --- | --- | --- |
| `queued` | `queued` | A reply was written; the detail is the planner's reason (`delayed`, `ongoing`, `postponed_night`, ...), or `adopted_newest` when the job adopted the newest message ([the maximum wait](#the-maximum-wait)), the row id is its first part, and `parts` is the number of parts |
| `skipped` | `switch_off`, `autopilot_stopped`, `autopilot_paused` | Step 1 |
| `skipped` | `contact_missing`, `contact_not_active` | Step 2 (`contact_missing` also when the generator no longer finds it) |
| `skipped` | `trigger_missing`, `trigger_not_incoming`, `trigger_before_resume`, `trigger_stale` | Step 3 |
| `skipped` | `superseded`, `answered` | Step 4 |
| `skipped` | `row_exists` | Step 5 |
| `skipped` | `no_messages` | The generator found no incoming message |
| `skipped` | `plan_not_possible` | Step 9 |
| `skipped` | `enqueue_refused` | Step 10; the detail is the refusal reason (`autopilot_disabled`, `contact_not_active`, `text_empty`, ...) |
| `skipped` | `provider_unavailable` | The cooldown pause itself could not be stored |
| `handed_over` | `text_trigger` | Step 6; the detail is the hand-over outcome |
| `handed_over` | `needs_owner`, `truncated`, `reply_invalid`, `prompt_too_large`, `mode_unavailable` | Step 7 |
| `stopped` | `persona_missing`, `provider_auth`, `provider_request` | Steps 7 and 8; the detail is the error class |
| `paused` | `provider_unavailable` | The last try failed transiently; the detail is the error class |

### The failure policy

| Failure | Retry | Pause | Stop | Hand-over | Notice |
| --- | --- | --- | --- | --- | --- |
| `LLMRateLimitError`, `LLMUnavailableError`, `LLMInvalidResponseError`, any unexpected exception | Yes: 60, 300, 900 s | After the 4th try, 15 min | No | No | One per new pause |
| `LLMAuthError` | No | No | `provider_auth` | No | One per new stop |
| `LLMRequestError` (402 included) | No | No | `provider_request` | No | One per new stop |
| `PersonaMissing` | No | No | `persona_missing` | No | One per new stop |
| `needs_owner` in the reply | No | No | No | Yes (model, no classes) | The hand-over notice |
| A truncated reply | Once, with twice the tokens | No | No | On the second truncation | The hand-over notice |
| `ReplyInvalid` | Once | No | No | On the second failure | The hand-over notice |
| `PromptTooLarge`, `DefaultModeUnavailable` | No | No | No | Yes | The hand-over notice |
| A stop-trigger in the burst | No | No | No | Yes (text, the classes) | The hand-over notice |
| `PlanNotPossible`, `EnqueueRefused`, `NoMessagesToReplyTo` | No | No | No | No | None |

A skip never sends a notice. A stop or a pause concerns the whole autopilot, not a contact; the sender
keeps sending rows that are already in the outbox, because they passed every check when they were
written and the sender checks the guard again. The notices go through the 15 s deadline
(`OWNER_NOTIFY_DEADLINE`); a failing or hung notifier changes nothing about the stop or the pause.

### Logs

Logger `cyrano.services.reply_job`: one INFO line `reply job finished` per run with the contact id, the
trigger message id, the attempt, the outcome, the reason, the detail, the row id and `parts` (the
number of parts of a queued reply, otherwise empty); the detail `adopted_newest` marks a run that
adopted the newest message; one INFO line
`reply job will be retried` with the error class and the delay instead when the job is retried;
WARNING `autopilot notice was not sent` with the class name. Logger `cyrano.services.autopilot`: INFO
`reply job enqueued` (ids and the delay), WARNING `the conversation pace was not decided` (contact id
and class name; the job is enqueued with the normal debounce), WARNING `reply job was not enqueued`
(class name), INFO
`autopilot sweep` (counters), ERROR `autopilot sweep failed` (class name). Never a message text, a
generated reply, a name, a username, a Telegram id or a photo path.

## The autopilot state

Table `autopilot_state`, migration `0009`: one row, `id` fixed to 1 by a CHECK constraint and seeded
idempotently. Columns `stopped`, `stop_reason`, `paused_until`, `pause_reason`, `updated_at`. It lives
in the database, so it survives a restart of the app and of the worker.

| Method | Effect | Returns |
| --- | --- | --- |
| `get` | Reads the row | The state |
| `stop(reason)` | Sets the stop; the first reason stays | Whether the autopilot was newly stopped |
| `pause_until(until, reason, *, now)` | Only ever extends a pause | Whether no pause was active at `now` |
| `resume()` | Clears the stop and the pause | The state before the call |

A missing row raises `AutopilotStateMissing`. The sender's own `sender_state` (CYR-22) is a different
row: a flood pause or an account stop stops sending, the autopilot state stops generating.

**Stop versus cooldown.** A stop is for a cause the owner must fix (a key, a balance, the persona); it
lasts until `autopilot resume --yes`. A cooldown is for a provider that is down or rate limiting; it
ends by itself after 15 min, and the sweeper then picks up the messages that are still younger than
the stale age.

## `reply_after`

`contacts.reply_after` (nullable, UTC) is the moment the owner last resumed the contact. The
autopilot never answers a message whose time (`created_at`, Telegram's send time) is at or before it,
and the burst and the sweeper ignore such messages too. A message that Telegram timestamped before the
resume but that the catch-up stored after it is therefore not answered.

| Path | `reply_after` | What the autopilot answers |
| --- | --- | --- |
| First activation, `pending -> active` from the card (`CardActions.reply`) | Left empty | The messages that arrived while the contact was `pending`, if the newest is not older than the stale age (the sweeper finds them) |
| Resume, `paused -> active` (`PauseActions.resume`, from the manual-pause or the hand-over notice) | The resume time | Only messages that arrive after the resume |

`ContactRepository.set_status(..., reply_after=...)` accepts the value only together with `active`
(`ReplyAfterRefused` otherwise), stores it only on a real change, and a same-status call keeps the
stored one.

## The sweeper

`AutopilotSweeper.run()` is a background task of the app, started with the other tasks once the
userbot is connected, strictly sequential. It sweeps once at start and then every
`SWEEP_INTERVAL_SECONDS` (60). While the switch is on and the autopilot is neither stopped nor paused,
it asks `ReplyBacklogRepository.awaiting_reply` for every `active` contact whose latest message is
incoming, younger than the stale age, newer than `reply_after`, at least twice the debounce old (all by
the message's Telegram time, the latest being the latest by `(created_at, id)`), and
without any outbox row for it (in any state), and enqueues the job through the same queue with no
delay. The job id is the same as the scheduler's, so a job that is still waiting is not doubled. A
failing sweep or queue is logged with the class name and never stops the loop.

**The summary scan (CYR-25).** A sibling task, `SummarySweeper` (`services/summary_sweeper.py`), is
started at the same place with the same cadence and is cancelled first on shutdown. While
`SUMMARY_ENABLED` is on, it enqueues one `update_contact_summary` job for every `active` or `paused`
contact with at least `SUMMARY_EVERY_MESSAGES` messages after its summary cursor and no recent
failure. It is independent of `AUTOPILOT_ENABLED` and of the autopilot stop and pause. Details:
[summaries.md](summaries.md#the-sweeper-and-the-worker-job).

## Constants and settings

| Name | Value | Meaning |
| --- | --- | --- |
| `AUTOPILOT_ENABLED` | `false` | The switch, read by the app, the worker and the sender's guard |
| `AUTOPILOT_DEBOUNCE_SECONDS` | 12 (0 to 300) | Delay of the job after a message when the conversation is not ongoing; the sweeper waits twice as long |
| `AUTOPILOT_DEBOUNCE_ONGOING_SECONDS` | 4 (0 to 300, at most `AUTOPILOT_DEBOUNCE_SECONDS`) | Delay of the job after a message in an ongoing conversation (CYR-43) |
| `AUTOPILOT_MAX_BURST_WAIT_SECONDS` | 30 (1 to 300, at least `AUTOPILOT_DEBOUNCE_SECONDS`) | Burst age from which a job adopts the newest message instead of skipping (CYR-43) |
| `REPLY_STALE_AFTER_SECONDS` | 6 h | The outbox's stale age; an older message is never answered |
| `SWEEP_INTERVAL_SECONDS` | 60 | Between two sweeps |
| `JOB_MAX_TRIES` | 4 | One run plus three retries |
| `RETRY_DELAYS_SECONDS` | 60, 300, 900 | Delay before the 2nd, 3rd and 4th try |
| `PROVIDER_COOLDOWN_SECONDS` | 900 | The pause after the last try failed |
| `TRUNCATION_RETRY_FACTOR` | 2 | Multiplies `LLM_REPLY_MAX_TOKENS` for the second generation |
| `TRUNCATION_MAX_TOKENS` | 1600 | Cap of that retry |
| `JOB_TIMEOUT_SECONDS` | 300 | Arq's limit for one try |
| `WORKER_MAX_JOBS` | 2 | Jobs one worker runs at the same time |
| `BURST_HISTORY_MESSAGES` | 20 | Messages read for the job's supersession check, the burst and the burst age |
| `ADOPTED_NEWEST_DETAIL` | `adopted_newest` | The detail of a run that adopted the newest message |

The worker runs with `max_tries=4`, `job_timeout=300`, `keep_result=0` (no result key, so the same job
id can be enqueued again after the job ended), `max_jobs=2`, `retry_jobs` on and the health check every
60 s.

## The worker process

`arq cyrano.worker.settings.WorkerSettings`. `startup` configures the JSON logging and builds, through
`build_worker_resources`: the engine and the repositories, the LLM factory and the `reply` client, the
`ReplyGenerator`, the schedule service and the planner, the `HandOverService`, a send-only
`NoticeBot` (an aiogram `Bot` without a dispatcher or polling; the owner notices and the hand-over
notice go through the existing notifier classes, and their buttons are answered by the app's polling
bot, which uses the same token), the `OutboxService` and the `ReplyJobRunner`. `shutdown` closes the
bot session, the HTTP client and the engine, in that order, each even if the one before failed.
`reply_to_contact(ctx, contact_id, trigger_message_id)` validates the arguments (`ReplyJobRequest`),
calls the runner and turns `RetryJob` into `arq.Retry(defer=...)`. A missing or invalid configuration
still ends the import with the readable `ConfigError` text.

## The commands

`python -m cyrano autopilot ...` runs on the host with the app `Settings` and the database up.

| Command | Makefile | What it does |
| --- | --- | --- |
| `autopilot status` | `make autopilot-status` | The switch, whether the autopilot is running, stopped (with the reason) or paused (until when, with the reason), the `autopilot` outbox rows per state, and the number of `active` contacts whose latest message is incoming and newer than `reply_after`. Never a text, a name or an id |
| `autopilot resume [--yes]` | `make autopilot-resume [YES=1]` | Clears a pause. A stop is cleared only with `--yes`; without it a one-line reminder to fix the cause first and exit 1, and nothing changes. Nothing to clear is a harmless line and exit 0 |

A bad command line prints the usage and exits 1; a storage failure prints `storage failed: <class>`
and exits 1; an invalid configuration exits 2.

## Checklist before switching it on

1. The schedule is imported and checked: `make schedule-import FILE=data/schedule.json`,
   `make schedule-show`, `make schedule-check`.
2. The persona and the style examples are imported: `make persona-import`, `make examples-import`,
   `make examples-stats`; `make reply-preview CONTACT=...` gives replies you would send.
3. The send limits (`SEND_LIMIT_*`) are reviewed for your account.
4. The notices work: you pressed Start in the control bot, and a test row
   (`make outbox-enqueue ...`) reached a test contact.
5. Test first with a third account of your own as the only `active` contact, and watch it: replies,
   timing, a stop-trigger, a manual message of yours, `make autopilot-status`.
6. Set `AUTOPILOT_ENABLED=true` in `.env` and restart both the app and the worker.

To switch it off, set `AUTOPILOT_ENABLED=false` and restart both. Rows already written stay in the
outbox and are cancelled by the sender's guard (`autopilot_disabled`).

## Not covered

- Rows stored before the message-time rule keep the insertion time they were stored with; nothing is
  migrated. An incoming row from that period may look younger than it was.
- The contact's age: nothing checks it.
- Other languages than Russian and a little English in the stop-trigger detector.
- Voice, video, stickers and other media: they are stored as an empty text (`[no text]` in the
  prompt) and the reply is written without knowing them.
- Proactive messages (CYR-23).
- A switch per contact (an open question).
