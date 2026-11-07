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
     |
     v
worker: ReplyJobRunner.run ---- skips, hand-over, stop or cooldown pause (tables below)
     |
     v
HumanizationPlanner.plan (send time) ----> OutboxService.enqueue (guard, text, row 'autopilot')
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
| `services/reply_job.py` | `ReplyJobRunner`, `AutopilotNotices`, `burst_texts`. No Arq import |
| `services/autopilot.py` | `AutopilotScheduler`, `AutopilotSweeper`, `autopilot_running` |
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

Every new incoming message of an `active` contact enqueues one job, deferred by
`AUTOPILOT_DEBOUNCE_SECONDS` (default 12). The job id is `reply-<contact id>-<message id>`, so the same
message is never queued twice while its job exists. A burst therefore makes one job per message, and
every job but the newest one finds a newer incoming message when it runs and skips (`superseded`).
Only the job of the last message of the burst generates, and the whole burst is in its prompt.

"Newer" means newer by the pair `(created_at, id)` and `created_at` is Telegram's send time for both
directions ([Message times](contact-lifecycle.md#message-times)). A message that a catch-up stores late
but that was written earlier is older than the trigger and never supersedes it, and a message written
many hours ago is old by its own time however recently it was stored.

## The job

`ReplyJobRunner.run(contact_id, trigger_message_id, *, attempt)` returns a `ReplyJobResult` (outcome,
reason, an optional detail and the row id) or raises `RetryJob(defer_seconds)` for a transient failure.
`attempt` is Arq's `job_try`. Every check reads fresh data; nothing is cached between jobs.

1. The switch is off, the autopilot is stopped, or a cooldown pause lasts beyond now: skip.
2. The contact is missing or not `active`: skip.
3. The trigger message is missing (or belongs to another contact), is not incoming, has a time at or
   before `contacts.reply_after`, or is older than the stale age by its Telegram time: skip.
4. Superseded: the latest message of the contact is not the trigger. A newer incoming message gives
   `superseded`, a newer outgoing one (the owner or the sender answered) gives `answered`.
5. An outbox row already exists for (contact, trigger), in any state: skip.
6. The stop-trigger detector runs on the burst: the run of consecutive incoming messages that ends
   with the trigger, only messages after `reply_after`. A hit calls `HandOverService.hand_over` with
   the classes and the source `text`.
7. `ReplyGenerator.generate` with the contact's mode and no summary:
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
9. `HumanizationPlanner.plan(contact_id, trigger_message_id, now)`; `PlanNotPossible` is a skip.
10. `OutboxService.enqueue(contact_id, text, send_at, autopilot, trigger_message_id)`; an
    `EnqueueRefused` is a skip with its reason as the detail. A duplicate enqueue returns the stored row.

Everything is idempotent: a job run twice, a retried job or a job that races the sweeper's job for the
same message writes one row at most (step 5 and the partial unique index of the outbox).

### Outcomes and reasons

| Outcome | Reason | When |
| --- | --- | --- |
| `queued` | `queued` | A row was written; the detail is the planner's reason (`delayed`, `ongoing`, `postponed_night`, ...) |
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
trigger message id, the attempt, the outcome, the reason, the detail and the row id; one INFO line
`reply job will be retried` with the error class and the delay instead when the job is retried;
WARNING `autopilot notice was not sent` with the class name. Logger `cyrano.services.autopilot`: INFO
`reply job enqueued` (ids and the delay), WARNING `reply job was not enqueued` (class name), INFO
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

## Constants and settings

| Name | Value | Meaning |
| --- | --- | --- |
| `AUTOPILOT_ENABLED` | `false` | The switch, read by the app, the worker and the sender's guard |
| `AUTOPILOT_DEBOUNCE_SECONDS` | 12 (0 to 300) | Delay of the job after a message; the sweeper waits twice as long |
| `REPLY_STALE_AFTER_SECONDS` | 6 h | The outbox's stale age; an older message is never answered |
| `SWEEP_INTERVAL_SECONDS` | 60 | Between two sweeps |
| `JOB_MAX_TRIES` | 4 | One run plus three retries |
| `RETRY_DELAYS_SECONDS` | 60, 300, 900 | Delay before the 2nd, 3rd and 4th try |
| `PROVIDER_COOLDOWN_SECONDS` | 900 | The pause after the last try failed |
| `TRUNCATION_RETRY_FACTOR` | 2 | Multiplies `LLM_REPLY_MAX_TOKENS` for the second generation |
| `TRUNCATION_MAX_TOKENS` | 1600 | Cap of that retry |
| `JOB_TIMEOUT_SECONDS` | 300 | Arq's limit for one try |
| `WORKER_MAX_JOBS` | 2 | Jobs one worker runs at the same time |
| `BURST_HISTORY_MESSAGES` | 20 | Messages read for the supersession check and the burst |

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
- Contact summaries: the prompt has no summary until CYR-25.
- Splitting a reply into several messages (CYR-36), proactive messages (CYR-23).
- A switch per contact (an open question).
