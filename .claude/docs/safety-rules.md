# Safety rules

**Every rule below says where it is enforced. All of them exist except S7 (limits on proactive
messages, CYR-23, planned).** S1 and S2 are enforced since
CYR-19: one guarded sender is the only way to a contact. Since CYR-17 the autopilot job writes `autopilot`
rows too, only with `AUTOPILOT_ENABLED=true`, and the same sender sends them. S8 is enforced since CYR-12,
S5 since CYR-22, S4 since CYR-21, and S6 since CYR-18, its splitting part since CYR-36. S3 and S10 are enforced
since CYR-20 for incoming texts, and since CYR-17 for the whole burst in the autopilot job and for the model
signal, on which the job hands over. S9 is enforced by ignore files, tests and review. Changing or
dropping a rule is a large change (see [CLAUDE.md](../../CLAUDE.md)) and ships with the code and the test
([CONTRIBUTING.md](../../CONTRIBUTING.md)). Statuses:
[contact-lifecycle.md](contact-lifecycle.md).

Tests named here exist unless a rule says they are planned. They use scripted fakes and fake data
only.

## S1. Only active contacts get replies

- **Rule:** an automatic reply is sent only to a contact in status `active`. The status is checked
  when the reply is scheduled and again right before it is sent.
- **Why:** replying to someone the owner has not approved is the worst failure of the product.
- **Enforced where:** exists (CYR-19). `services/send_guard.py` runs twice: in
  `OutboxService.enqueue`, which refuses without storing anything, and in `OutboxSender` right before
  sending, on a contact it has just re-read; a refusal there cancels the row and nothing is sent. An
  `autopilot` row also needs `AUTOPILOT_ENABLED`; a `manual_test` row does not, but the status check
  applies to it. Since CYR-17 the status is also checked when the app enqueues a reply job
  (`AutopilotScheduler`) and when the job runs (`ReplyJobRunner`, which also skips messages at or before
  `contacts.reply_after`, older than 6 h, or already answered). Details:
  [outbox-and-sender.md](outbox-and-sender.md), [autopilot.md](autopilot.md).
- **How it is tested:** exists. `tests/services/test_send_guard.py` runs the guard over every status,
  source and switch value and an unknown contact, and proves a refused enqueue stores nothing.
  `tests/services/test_outbox_sender.py` proves on real PostgreSQL that a contact that became `paused`
  or `ignored` after the enqueue, a `new` or `pending` contact, a contact missing at send time and an
  `autopilot` row with the switch off all get no transport call. `tests/services/test_reply_job.py`,
  `test_autopilot.py` and `test_autopilot_end_to_end.py` (CYR-17) prove that the job and the scheduler
  do nothing for a contact that is not `active`, with the switch off, or while the autopilot is stopped
  or paused, and that a manual message between the job and the send cancels the reply.

## S2. One sender is the only way out

- **Rule:** every outgoing message goes through the sender. No other code path sends. This
  includes replies, proactive messages and reminders to contacts.
- **Why:** guards, limits and pacing can only be trusted if they cannot be bypassed.
- **Enforced where:** exists (CYR-19). The transport, `MessageTransport`, has exactly three narrow
  write methods (`send_text`, `show_typing`, `mark_read`; the last two since CYR-18). `OutboxSender` in
  `services/outbox_sender.py` is the only caller of all three, and `TelethonAdapter` in
  `userbot/adapter.py` is the only module with a Telethon write call. The read Protocol `UserbotClient`
  has no write method and no generic request call, so code typed against it cannot send. Telethon is
  imported only inside `userbot/`. A send with an unknown outcome is never retried automatically,
  because the contact could receive the message twice: the transport is called once for the row, and
  the owner checks the chat and decides. The limits in the sender exist since CYR-22 (see S5), and its
  humanized pacing since CYR-18 (see S6). Since CYR-44 the rule covers the sender's second coroutine,
  `run_early_reads`: the early read mark is a request registered by the intake side
  (`services/early_read.py`, which cannot reach the transport) and executed by the sender, so the
  module of the sender is still the only caller of `mark_read`.
- **How it is tested:** exists, as structural tests that fail on a new way out.
  `tests/test_one_sender.py` scans the syntax tree of all of `src`: only the sender module calls
  `send_text`, `show_typing` and `mark_read`, also through `getattr`, and only on its transport; only
  the adapter and the Protocol define them; outside `bot/` only the adapter uses a Telethon write method
  (`send_message`, `send_file`, `forward_messages`, `edit_message`, `delete_messages`,
  `send_read_acknowledge`, `action` and similar), the sender's `mark_read` on the transport being the
  one allowed use of such a name elsewhere; inside `bot/` only the two owner-chat modules send; Telethon
  request classes are imported only by the adapter and only from the allowlist (`GetFullUserRequest`);
  nothing outside `userbot/` imports Telethon and nothing outside `bot/` imports aiogram; `UserbotClient`
  has no write member; `MessageTransport` has exactly `send_text`, `show_typing` and `mark_read`. Since
  CYR-44 more scans: the early read registry, its scheduler and its domain module never reference a
  transport method, the `MessageTransport` Protocol or `_transport`; `mark_read` is called inside the
  sender only from `_before_send` and `_execute_early_read`; that function calls no other transport
  method, never touches the outbox or the limiter, and the sender module holds no lock. A last
  test proves the scans catch a planted violation.
  `tests/userbot/test_never_sends.py` still compares the members of `UserbotClient`, `IncomingEvent` and
  `SenderLike` with an allowlist, scans every userbot module for write-looking names, with the adapter's
  `send_message`, `send_text`, `send_read_acknowledge`, `action` and `mark_read` and `send_code_request`
  in `login.py` (it asks Telegram for a login code) as the only allowed ones, and runs the handler tests
  with fakes whose write methods record the attempt and raise.

## S3. Stop-triggers hand over

- **Rule:** meeting or date proposals, phone and contact exchange, money, photo requests, a direct
  question whether it is a bot or AI (see S10), the owner's exes or past relationships, self-harm
  or suicide, and a message the model cannot answer without inventing facts stop the conversation:
  the contact becomes `paused` with `pause_reason = stop_trigger` and the owner is notified. The
  assistant does not answer these itself.
- **Why:** these are the points where a wrong answer costs the owner something real.
- **Enforced where:** exists (CYR-20). `IntakeService` runs the pure detector
  (`services/stop_triggers.py`, pattern data in `stop_trigger_patterns.py`, hard rule 4) on every
  new incoming message of an `active` contact, right after storing it; a hit calls
  `HandOverService.hand_over`, which pauses only an `active` contact, and only a real status change
  cancels the scheduled rows (reason `stop_trigger`) and sends the owner ONE notice with the last 5
  lines and the buttons resume, keep and ignore, under a 15 s deadline. A failing notice never undoes
  the pause; the gate never raises into the intake. A row already `sending` is refused by the
  sender's second guard; the paused status is honoured by S1. The reply instructions tell the model
  to answer with exactly `NEEDS_OWNER` on the same topics; the generator turns the signal (exact or
  embedded) into `needs_owner` with an empty text, so no text of such a reply can be sent. Low model
  confidence has no numeric threshold; it is this signal. Since CYR-17 the autopilot job hands over on
  the signal (source `model`, no classes) and writes no row, and runs the detector again on the whole
  burst before it calls the model. Details: [stop-triggers.md](stop-triggers.md),
  [autopilot.md](autopilot.md).
- **How it is tested:** exists. `tests/services/test_stop_triggers.py` with
  `tests/services/stop_trigger_phrases.py`: at least 15 positive and 15 close but harmless phrases
  per class, the accepted false positives, case, `ё`, repeats, punctuation, emoji, invisible and
  combining characters, spaced letters, phone formats and separators, card runs, e-mails, handles,
  links, messenger names only with a verb, several texts, huge and odd input and a seeded fuzz, and
  the validity of the pattern data. `tests/services/test_hand_over.py` on real PostgreSQL: the pause
  with its reason, only the contact's rows cancelled with the reason, one notice with the lines in
  order and truncated, nothing reaching the contact through a real `OutboxSender` with a transport
  that fails on any call, non-active and paused contacts left alone without a status write, a
  second trigger and a lost race without a notice, failing and hung notifiers, failing cancel and
  history steps, and the privacy of the logs at DEBUG. `tests/services/test_intake_stop_triggers.py`:
  the gate for each status, only the new message checked, a replay handed over once, a failing
  hand-over or detector without a raise or a retry of the userbot handler.
  `tests/services/test_reply_needs_owner.py` and `tests/cli/test_reply_cli.py`: the signal in every
  case and wrapping, embedded, a normal reply untouched, the usage kept, the log flags.
  `tests/bot/test_hand_over_notice.py` and `tests/bot/test_handlers.py`: the notice to the owner only,
  escaping, the line limit, the three buttons, keep and ignore, double clicks, strangers.
  `tests/test_one_sender.py` proves that `HandOverNotifier` has one method, is implemented only in
  `bot/`, is called only by `services/hand_over.py` behind the deadline, that the hand-over, the
  intake, the detector and the `stop-check` command touch no transport method, that only
  `services/hand_over.py` sets `STOP_TRIGGER`, and that the detector imports only the domain and its
  data.

## S4. A manual message of the owner pauses the contact

- **Rule:** if the owner writes in the chat by hand from the second account, the contact becomes
  `paused`.
- **Why:** the owner has taken over; the assistant must not talk over them.
- **Enforced where:** exists (CYR-21). A second Telethon handler (`userbot/outgoing_handler.py`)
  passes every outgoing private message to a user to `ManualMessageDetector`
  (`services/manual_intervention.py`). A message whose id is already stored is the sender's own and is
  ignored; when the contact has a row in state `sending` the detector first waits, with early exit,
  for the sender to store the id (0.25, 0.5, 1, 2, 4 and 8 s). A message that is not the sender's own
  pauses an `active` contact with `pause_reason = manual_message`; only a real status change
  cancels the scheduled rows (reason `manual_message`) and sends the owner one notice with a Resume
  button. A contact in any other status only gets the message stored. A row already `sending` cannot
  be cancelled by the detector; the sender's second guard, right before the send call, refuses it
  because the contact is no longer `active`. A failure of the notice never undoes the pause. A missing or naive time of the event does not
  stop the pause (CYR-40): the message is stored with the time of the one shared rule
  (`services/message_time.py`). Resume
  is `PauseActions.resume`, reachable only from the owner-only control bot (S8). Details:
  [contact-lifecycle.md](contact-lifecycle.md#manual-intervention-cyr-21).
- **How it is tested:** exists. `tests/services/test_manual_intervention.py`, on real PostgreSQL with
  an injected sleep: a manual message to an `active` contact pauses it with the reason, cancels only
  its scheduled rows, stores the message with Telegram's time (or with now when the time is missing, naive or more than
  5 minutes ahead of the injected clock) without moving `last_message_at` and notifies once; every other status is only stored; an unknown peer changes nothing; the same event
  twice pauses and notifies once; the sender's own echo is ignored, also when it arrives before the
  id is stored (each delay position); a manual message during a `sending` row waits every delay and
  pauses; a manual message during the typing action of a real `OutboxSender` makes the second guard
  cancel the row with `contact_not_active` and the transport sends nothing; a failing or hanging
  notifier keeps the pause; the privacy test captures all logs at DEBUG. `tests/userbot/` covers the
  filter, the handler (retry, no retry after a pause, group, channel, bot and service rejections) and
  that both handlers are registered before connecting and removed on shutdown;
  `tests/services/test_pause_actions.py` and `tests/bot/` cover resume and the notice;
  `tests/test_one_sender.py` proves that `ManualPauseNotifier` has one method, is implemented only in
  `bot/`, is called only by the detector behind the deadline, and that the detector touches no
  transport method.

## S5. Rate limits and FloodWait

- **Rule:** sending is rate limited centrally, and Telegram's FloodWait is handled in the sender
  by waiting or giving up cleanly, never by hammering.
- **Why:** account limits and bans are a main risk.
- **Enforced where:** `OutboxSender` and `services/send_limiter.py` (CYR-22). Before every send the
  six limits (global and per contact; minute, hour, day) are counted from the stored outgoing
  messages; a reached limit puts the row back at the time a slot frees. The limiter itself never drops
  a row; a row older than 6 h past its originally intended time (`due_at`) is cancelled as `stale`
  when it is claimed, and the owner is told in one summary notice (the count, at most hourly). A flood
  wait pauses the whole sender until now + the wait + 5 s, and the pause is stored in `sender_state`,
  so it survives a restart. An account-level error (the lost or restricted session, a frozen account,
  a bare `UnauthorizedError` and the like) stops the sender until the owner runs
  `outbox resume --yes`; while it is stopped the owner gets a reminder every 6 h. Details: [outbox-and-sender.md](outbox-and-sender.md).
- **How it is tested:** `tests/services/test_send_limiter.py` proves that each limit blocks exactly at
  its threshold, the boundary, the latest free time over several limits, and that contacts do not mix.
  `tests/services/test_outbox_sender_safety.py` proves on real PostgreSQL that a limited row is
  rescheduled and sent once the clock passes, that no row is claimed and the transport is not called
  during a pause or a stop, that both survive a new sender instance, that a shorter pause never
  shortens a longer one, and that each notice comes once, the stale summary counts several cancels into
  one notice and respects its hourly interval, and the stop reminder repeats every 6 h while stopped,
  never after a resume and never for a mere pause. `tests/test_one_sender.py` proves that only
  the flood path and the limit path reschedule, only the flood path pauses, only the account path
  stops and only the owner's command resumes.

## S6. Humanized pacing and quiet hours

- **Rule:** replies are delayed, show a typing action, are split into several messages when the
  owner's style is several short messages in a row, and are not sent during quiet hours.
- **Why:** instant, uniform replies at any hour look automated and draw limits.
- **Enforced where:** exists (CYR-18; the splitting since CYR-36). `plan_send_time` in
  `services/humanization.py` proposes the delayed time; the autopilot job calls it since CYR-17. The
  sender, `services/outbox_sender.py`, enforces the rest for itself: an `autopilot` row inside a night or
  busy window goes back to `scheduled` with `error_class = outside_hours`, never dropped, and every row
  goes through the read mark, a pause and typing before the send. A failed read mark or typing never
  blocks the send, except a flood wait or an account problem, handled as on a send. The windows use the
  profile's time zone, by default `TIMEZONE`. Since CYR-36 a reply the model wrote as several lines
  is stored as a chain of up to three rows (`split_reply`, `OutboxService.enqueue_sequence`); the sender
  claims a part only after the one before it was sent, waits `split_pause_seconds` after each send, runs
  only typing for a later part (no read mark, no read pause, no availability check, because the reply
  has started) and cancels the rest of a reply whose part failed or was cancelled. Every part passes the
  guard, the limits and the stale rule. Since CYR-44 the read mark of an incoming message may come
  earlier than the reply, through the sender's second coroutine, only while the autopilot and the sender
  run, the contact is `active`, the conversation is ongoing and the moment is outside the night and busy
  windows; it is not a message and is not counted by the send limits. Details:
  [humanization.md](humanization.md#the-early-read) and
  [outbox-and-sender.md](outbox-and-sender.md#multi-part-replies).
- **How it is tested:** exists. With an injected clock and a seeded random generator:
  `tests/services/test_humanization.py` (windows, reasons, delays, typing),
  `tests/services/test_humanization_planner.py` (the burst, on real PostgreSQL) and
  `tests/services/test_outbox_sender_humanization.py` (the order, a row due in a window is deferred and
  sent after, not dropped, `manual_test` rows skip the check, cosmetic failures, a flood wait and an
  account problem before the send, the deadline, the relation of the stale-sending age). Splitting:
  `tests/services/test_reply_split.py` (the pure split), `tests/db/test_outbox_sequence_repository.py`
  (the chain, the claim rule, the orphans, on real PostgreSQL) and
  `tests/services/test_outbox_sender_parts.py` (the order of the steps, the split pause from the seeded
  generator, a failure, a limit, supersession and a manual message between the parts, a part due in a
  busy window).

## S7. Limits on proactive messages

- **Rule:** proactive messages are limited. After two unanswered proactive messages the contact
  becomes `paused`. They go through the sender like every other message.
- **Why:** unsolicited messages are the fastest way to complaints and bans.
- **Enforced where:** proactive messages (CYR-23, planned). The sender's send limits exist since
  CYR-22 (see S5), and a proactive message will count against them like every other message. The
  remaining limits are open questions in [decisions.md](decisions.md).
- **How it is tested:** the second unanswered proactive message pauses the contact; an answer
  resets the counter; a paused contact gets no proactive message.

## S8. The control bot is owner-only

- **Rule:** the control bot answers only `OWNER_TELEGRAM_ID`. Nobody else can approve contacts,
  change modes, resume or ignore.
- **Why:** the card buttons are the approval mechanism; whoever can press them controls the
  account.
- **Enforced where:** exists (CYR-12). `OwnerOnlyMiddleware` in `bot/middleware.py` is an outer
  middleware on every update of the dispatcher: an update whose user is not `OWNER_TELEGRAM_ID`,
  or has no user, is dropped before any handler runs, with no reply, no database access and no id
  in the logs (only a counter). The `/start` handler also answers private chats only, and the card
  sender takes the owner id from `Settings` and has no way to address another chat.
- **How it is tested:** exists in `tests/bot/test_handlers.py`: a message and a button press from
  another user id get no API call at all, change no status and leave no id or text in the logs; a
  message without a sender is dropped; `/start` from the owner works; every recorded outgoing call
  of the handler and sender tests targets the owner (`tests/bot/test_sender.py`).

## S9. No personal data in logs or git

- **Rule:** message text, names and other personal data are never logged at INFO or above. Persona
  text, style examples, sessions, media and messages never go to git; fixtures use fake data.
  Collected profile data is personal data too: username, display name, bio, the profile photo,
  the card photos (a list), their paths, and the matching-bot texts are never logged at INFO or above and never go to git.
  The collector logs internal contact ids, part names, error class names and counters only.
  Prompts and model replies contain personal data: the LLM client never logs a prompt, a reply
  (valid or not) or a response body at any level, and its errors carry the status code or the
  exception class only. A reply prompt (CYR-16) holds the persona, the style examples, the
  contact's name, age, city, about text and bio, and the last messages of the conversation; it
  never holds the username, a Telegram id, an internal id, a photo path or a raw Leomatch text. The
  reply generator logs counters only, the errors of the reply pipeline carry ids and counts, and
  `reply-preview --show-prompt` prints the prompt to the terminal only, with a warning. Its log lines carry the step, the model, token counts, the duration and
  retry counters. It never logs the API key.
  The grammatical gender (CYR-46) is personal data about a person: it is never logged, neither alone nor
  next to a name or id; the handler, the actions and the command log ids and outcomes only, and the
  setting `DEFAULT_CONTACT_GENDER` is withheld from the startup line.
  The persona text and the style examples are personal data too (CYR-14): they are never logged
  at any level and never go to git. The import commands and the services print and log counts,
  paths and JSON-path style locations of problems, never a persona or example text; `persona show`
  prints the length and the update time only and `examples stats` prints counts per tag. The real
  files live only in `data/`.
  A Telegram Desktop chat export is personal data too (CYR-34): it is converted locally by a
  command that reads a file and writes a file, never logs or prints a message text, a name, a
  sender id or a number (only counts), and writes the draft only under `data/` unless the owner
  passes `--allow-any-path`.
  Owner notifications about the outbox (CYR-19, CYR-22) carry internal ids, class names, seconds and counts only.
  The manual-pause notice (CYR-21) carries the internal contact id only.
  The autopilot notices (CYR-17, stopped and paused) carry the reason in plain words, the error class
  name and the minutes of a pause only: no id, no text, no name. The reply job and the sweeper log
  internal ids, outcomes, reasons, counters and class names, never a message text or a generated reply.
  The hand-over notice (CYR-20) is the one exception: it carries the display name (at most 60
  characters) and the last 5 message texts (at most 200 characters each), HTML-escaped, because the
  owner must see what to decide on. It goes only to the owner chat through the owner-only control
  bot and is never logged; the hand-over logs the contact id, the class values and the source, and
  the `stop-check` command prints class names only.
  The contact summary (CYR-25) is personal data: it is stored in the database (`contacts.summary`)
  and sent to the provider inside the extraction prompt and the reply prompt, and it is never
  logged at any level: the updater, the sweeper and the job log the contact id, outcomes, reasons,
  counters and class names only. It never contains sensitive attributes (health, politics,
  religion, sexuality, exact addresses, document numbers) by instruction, and never contact details
  (phone-like digit runs, e-mail addresses, links, @handles) by instruction and by code: the
  sanitiser removes them before anything is stored ([summaries.md](summaries.md#sanitising)). Only
  `summary show` prints its content, to the terminal, after a warning; the owner can remove it
  with `summary clear --yes`.
- **Why:** the data belongs to real people.
- **Enforced where:** exists today: `.gitignore` and `.dockerignore` exclude `.env`, `*.session`,
  `media/` and `data/` (profile photos live under `MEDIA_DIR`, `.env.example` and compose put it
  in `data/media` and `/data/media`, `media` is the default); `.gitignore` also ignores
  `persona*.md` and `style-examples*.json` anywhere, except the two invented samples in
  `examples/` and the guide, which are un-ignored by their exact names (CYR-14); secrets are `SecretStr` and masked in logs; `Settings.safe_summary()`, the
  only settings in the startup log lines, returns an allowlist (`LOGGED_SETTINGS`) with no secrets,
  no `OWNER_TELEGRAM_ID` and no `TELEGRAM_API_ID`. Planned: scrubbing of free-text
  logs and observability (CYR-28); until then, review enforces it (hard rules 8 and 9).
- **How it is tested:** exists today: tests assert that neither secret values nor the owner id and
  API id appear in the app and worker startup output or in `safe_summary()`, and that every
  setting is either allowlisted or deliberately withheld; the collector tests assert that no
  username, name, bio, Leomatch text, Telegram id or photo path appears in captured log output,
  failures included. Exists for the reply pipeline of the autopilot (CYR-17):
  `tests/services/test_reply_job.py` runs the job with all loggers at DEBUG and asserts that the
  generated reply, the message texts, the persona, the name, the username and the Telegram id are
  absent from the JSON-formatted logs and the owner notices. Exists for the LLM client
  (`tests/llm/test_logging.py`, `tests/llm/test_complete_json.py`): all log output is captured at
  DEBUG during retries, invalid JSON replies and failures, and the prompt, the reply, the response
  body and the key are asserted absent; the raw invalid reply is also absent from the raised error.
  Exists for the persona and the examples (CYR-14): validation errors carry no input values, every
  file error is asserted to contain no marker text, the commands' output is asserted to contain no
  sample text, and a full import of the sample files with all loggers at DEBUG must leave no sample
  text in the captured logs or in the raised errors; the `.gitignore` and `.dockerignore` content
  is tested too. Exists for the reply pipeline (CYR-16): the generator is run with all loggers at DEBUG and the
  persona, message, reply, bio and username markers, the Telegram id and the summary text are
  asserted absent from the captured logs; the facts model and the rendered prompt are asserted to
  carry no username, id, path or raw Leomatch text; the preview command's errors are asserted to
  contain none of those texts. Exists for the converter (CYR-34): the captured output, errors and logs of a full
  run and of the failures contain none of the invented names, ids, texts or numbers, and the
  written draft contains none of the redacted strings. Exists for the summaries (CYR-25): the
  updater, the sweeper, the job and the commands run with all loggers at DEBUG and the
  JSON-formatted logs must hold none of the invented message texts, names, summary values or
  markers; `summary update` prints counts only. Numeric markers in these tests (fake
  Telegram ids, API ids, phone numbers, login codes, internal ids) must be distinctive and at least
  12 digits long, taken from `tests/privacy.py`, because the captured log records carry timestamps
  and thread and process ids that a short number can match by chance; `assert_absent` there refuses
  a numeric marker shorter than 10 digits.

## S10. The assistant never answers a direct bot question

- **Rule:** when a contact asks directly whether it is a bot or AI, the assistant does not answer.
  It stops the conversation and notifies the owner. This is a stop-trigger (S3).
- **Why:** a wrong or evasive answer is worse than silence, and the owner decides what to say.
- **Enforced where:** exists (CYR-20): the class `bot_question` of the detector, and the reply
  instructions, which tell the model to answer such a question with `NEEDS_OWNER` and never to
  claim to be human or deny being an AI.
- **How it is tested:** exists. Direct questions in many phrasings and spellings fire
  `bot_question`, an active contact is `paused` and the owner notified, and nothing is sent; the
  prompt tests pin the instruction and that no instruction claims a human identity.
