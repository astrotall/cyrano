# Contact lifecycle

The storage and the transition rules exist (CYR-9), and so does the registration of unknown senders
(CYR-10): the userbot creates contacts with status `new` and stores their incoming messages. The
collection of the profile for the owner's card exists too (CYR-11): it fills `profile` and changes
no status. The owner's card exists since CYR-12: delivering it moves `new` to `pending`, and its two
buttons move `pending` to `active` or `ignored`. The outbox and the guarded sender exist since
CYR-19 ([outbox-and-sender.md](outbox-and-sender.md)), and only the owner's test command can add a
row. Since CYR-22 a recipient rejection moves an `active` contact to `paused`. Since CYR-21 a message
the owner writes by hand to a contact from the second account does the same, and the owner can resume
from the notice ([Manual intervention](#manual-intervention-cyr-21)). Since CYR-17 the autopilot
answers `active` contacts automatically, but only with `AUTOPILOT_ENABLED=true`
([autopilot.md](autopilot.md)). The other flows that drive the lifecycle are **planned**, each
marked with its ticket.

## Statuses

`new -> pending -> active -> paused / ignored`

| Status | Meaning | Automatic replies | Set by |
| --- | --- | --- | --- |
| `new` | Registered by the userbot from an unknown sender | Never | userbot (CYR-10, exists) |
| `pending` | A card was delivered to the owner, waiting for a decision | No | card announcer (CYR-12, exists) |
| `active` | The owner pressed Reply and chose a mode | Yes: the autopilot writes the reply (CYR-17, only with `AUTOPILOT_ENABLED=true`) and the sender sends it (CYR-19) | owner (CYR-12 exists, modes CYR-15) |
| `paused` | Stopped by Telegram refusing to deliver to the contact, by a stop-trigger, by a manual message of the owner, or by the owner. The column `pause_reason` says which | No | sender (CYR-22, exists), manual-message detector (CYR-21, exists), hand-over on a stop-trigger (CYR-20, exists) or owner |
| `ignored` | The owner pressed Ignore on the card or on the hand-over notice | Never; silence forever | owner (CYR-12, CYR-20, exist) |

Only `active` is allowed to receive an automatic reply. See [S1](safety-rules.md).

## Transitions

**Decided in CYR-9.** The rules exist as data plus one function in
`src/cyrano/domain/transitions.py`; `ContactRepository.set_status` enforces them under a row lock.
The triggers marked "exists" are implemented; the others are planned. Changing the matrix is a large change (see
[CLAUDE.md](../../CLAUDE.md)).

| From | To | Trigger | Who or what | Trigger ticket |
| --- | --- | --- | --- | --- |
| (none) | `new` | An unknown sender writes to the second account | userbot | CYR-10 (exists) |
| `new` | `pending` | The card is delivered to the owner (the sender returned success) | card announcer | CYR-12 (exists) |
| `pending` | `active` | Owner presses Reply and chooses a mode; `reply_after` stays empty | owner, through the card | CYR-12 (exists), modes CYR-15 |
| `pending` | `ignored` | Owner presses Ignore | owner, through the card | CYR-12 (exists) |
| `active` | `paused` | Telegram refused a send because of the recipient (blocked, privacy, deactivated, not found, forbidden) | sender | CYR-22 (exists) |
| `active` | `paused` | A stop-trigger fires on a new incoming message (`pause_reason = stop_trigger`) | `HandOverService` | CYR-20 (exists) |
| `active` | `paused` | A voice message, a circle, a photo, a video or another attachment arrives; the assistant cannot see it (`pause_reason = stop_trigger`, source `media`) | `HandOverService`, through the media gate of the intake | CYR-58 (exists, interim) |
| `active` | `paused` | The owner writes a manual message in the chat from the second account | manual-message detector | CYR-21 (exists) |
| `active` | `paused` | The owner pauses the contact (`pause_reason = owner` exists in the enum; nothing sets it yet) | owner | none yet |
| `active` | `ignored` | The owner ignores the contact | owner | none yet |
| `paused` | `active` | Owner presses Resume on the manual-pause or the hand-over notice; `reply_after` is set to the resume time (CYR-17) | owner, through `PauseActions` | CYR-21, CYR-20 (exist) |
| `paused` | `ignored` | Owner presses Ignore on the hand-over notice | owner, through `PauseActions.ignore` | CYR-20 (exists) |

Rules:

- A new contact always starts as `new`; `ContactCreate` has no status field.
- `ignored` is terminal: nothing leaves it.
- Requesting the current status is a no-op, not an error: nothing is written, `updated_at` does not
  move, and a `mode_id` passed along is not applied.
- Every other transition raises `InvalidStatusTransition` and changes nothing.
- A contact becomes `active` only with a mode: the mode passed to `set_status`, or the one already
  stored (`set_mode`, or an earlier activation when resuming from `paused`). Otherwise
  `ActivationWithoutMode`, a subclass of `InvalidStatusTransition`, is raised. A `mode_id` that does
  not exist raises `ModeNotFound`.
- `mode_id` is applied only by a call that really activates the contact; other calls ignore it.
- `pause_reason` (`manual_message`, `send_rejected`, `stop_trigger`, `owner`) may be passed only together with
  `paused`; with any other status `PauseReasonRefused` is raised. It is stored by a call that really
  moves the contact into `paused` and cleared by every call that moves it out of `paused`. A
  same-status request is a no-op, so a second pause keeps the first reason. A contact paused before
  CYR-21 has no reason (`NULL`). `set_status` returns the contact plus a `changed` flag, true only
  when the status really changed; a caller that must act once per real change (the notice of a
  manual pause, the hand-over notice) uses that flag.
- `reply_after` (CYR-17) may be passed only together with `active`; with any other status
  `ReplyAfterRefused` is raised. It is stored by a call that really activates the contact, a
  same-status call keeps the stored value, and nothing clears it. `PauseActions.resume` always passes
  the resume time; the first activation from the card passes nothing, so it stays empty. The autopilot
  never answers a message whose time is at or before it. See
  [autopilot.md](autopilot.md#reply_after).

A contact may keep a mode that is later disabled. The mode is resolved when it is used:
`ModeRepository.resolve_by_id` and `resolve_by_name` return the requested mode if it is enabled and
otherwise the `default` mode. If `default` itself is missing or disabled they raise
`DefaultModeUnavailable`, so the caller stops instead of guessing a style.

## Message flow

| Step | What happens | Ticket |
| --- | --- | --- |
| 1 | An unknown sender writes to the second account; the userbot registers a contact (`new`) and stores the message. No reply is sent. Exists, see [Registration](#registration-cyr-10) | CYR-10, CYR-9 |
| 2 | The profile collector gathers nick, name, bio, photo and the matching-bot profile text if present, stores them in `profile` and publishes a `ContactReadyEvent`. Exists, see [Profile collection](#profile-collection-cyr-11) | CYR-11 |
| 3 | The card announcer sends the owner a card with a Reply button per enabled mode and an Ignore button through the control bot; after the card is delivered the contact becomes `pending`. It starts from the `ContactReadyEvent` or from the sweep. Exists, see [Card announcement](#card-announcement-cyr-12) | CYR-12 |
| 4 | On Reply the contact becomes `active` with the chosen mode; on Ignore it becomes `ignored`. Exists | CYR-12 |
| 5 | Stop-triggers: the new incoming message of an `active` contact is checked right after it is stored; on a hit the contact becomes `paused`, its scheduled rows are cancelled, the owner is notified and nothing is enqueued. Exists, see [Stop-triggers](#stop-triggers-cyr-20) | CYR-20 |
| 5a | Media gate: right after the stop-trigger check, a new incoming message of an `active` contact with a voice message, a circle, a photo, a video or another attachment (not a sticker) hands the contact over the same way, with the source `media`; the autopilot and the early read that follow find the contact paused and do nothing. Exists, see [Media messages](#media-messages-cyr-58) | CYR-58 |
| 6 | Each incoming message from an `active` contact enqueues one delayed Arq job (`AUTOPILOT_DEBOUNCE_ONGOING_SECONDS` in an ongoing conversation, otherwise `AUTOPILOT_DEBOUNCE_SECONDS`, CYR-43); of a burst one job generates, the others find a newer message and skip until the burst has waited `AUTOPILOT_MAX_BURST_WAIT_SECONDS`, after which a job answers the newest message; a row that an earlier job already wrote is cancelled as `superseded` by the newer job and by the sender while it has not started. A sweep every 60 s enqueues what was lost. Exists, behind `AUTOPILOT_ENABLED`, see [autopilot.md](autopilot.md) | CYR-17 |
| 7 | When the job runs it re-checks the switch, the autopilot state, the status, `reply_after`, the age, supersession and an existing row, runs the stop-trigger detector on the burst again, and only then generates; anything but `active` ends the job with no reply. The model signal hands the contact over | CYR-17, CYR-20 |
| 8 | The layered prompt is built: persona, mode fragment with few-shot examples, contact summary, last N messages | CYR-14, CYR-15, CYR-16, CYR-25 |
| 9 | The LLM is called through the LLM interface; the response is validated | CYR-13 |
| 10 | The job asks the planner for the send time ([humanization.md](humanization.md)) and writes an `autopilot` row, or a chain of up to three when the reply has several lines, one message per line (CYR-36). The sender sends it: status guard, FloodWait handling, rate limits, the read mark, typing and quiet hours, the parts in order (see [outbox-and-sender.md](outbox-and-sender.md)). In a lively conversation the chat was already marked read soon after the message by the early read ([humanization.md](humanization.md#the-early-read)); the read mark of the pre-send part then repeats it harmlessly | CYR-19, CYR-22, CYR-18, CYR-17, CYR-36, CYR-44 |
| 11 | Background: contact summaries of `active` and `paused` contacts every `SUMMARY_EVERY_MESSAGES` messages (exists, [summaries.md](summaries.md)); proactive messages to an `active` contact after a long silence after our last message (exists behind `PROACTIVE_ENABLED`, [proactive.md](proactive.md)); reminders for undecided cards (planned) | CYR-25, CYR-23, CYR-24 |

Stop-triggers are detected three times: by the pattern detector on the incoming text (step 5), by
the same detector on the whole burst in the job (step 7), and by the model's `NEEDS_OWNER` signal in
the generated reply (after step 9). Low model confidence is that signal; there is no numeric
threshold.

## Registration (CYR-10)

Exists. The userbot listens on the second account and passes every real private message to
`IntakeService.handle` in `services/intake.py`. The intake only listens and stores: it never sends,
never marks anything as read itself and never answers anyone ([S2](safety-rules.md)). Since CYR-44 it
registers a request for an early read mark as its last step for an `active` contact; the sender's second
coroutine executes it ([humanization.md](humanization.md#the-early-read)).

What counts as a message to register: an incoming message in a private chat with a real user.
Rejected without any effect, only counted in a DEBUG log: own (outgoing) messages, groups,
channels, anything that is not a private user chat, bots, deleted accounts, the Telegram service
account (id 777000), the account itself, an event whose sender cannot be read.

Steps of `IntakeService.handle`:

| Step | What happens |
| --- | --- |
| 1 | NUL bytes are removed from the text, the username and the display name. PostgreSQL rejects them in text and JSONB, so this happens before any repository call |
| 2 | The contact is read by `tg_id`. If it is missing, it is created with status `new`; there is no transition to `pending` here (the card announcer makes it, CYR-12) |
| 3 | The message is appended as `incoming` with Telegram's send time as `created_at` (see [Message times](#message-times)); the unique `(contact_id, tg_message_id)` makes a redelivery a no-op |
| 4 | `last_message_at` is touched with the stored `created_at` of that message; it only moves forward |
| 5 | Only if this call created the contact: a `NewContactEvent` (contact id, message id) is published |

- **What is stored.** On creation the contact gets `tg_id`, `username` and `display_name` (first and
  last name joined) from the first message; both may be missing. Later messages do not update them;
  the profile collection (CYR-11) refreshes them once from Telegram. A message from a known contact is stored whatever its
  status is (`pending`, `active`, `paused`, `ignored`); only registration is stored here, nothing
  decides about a reply.
- **Media-only messages.** A photo, sticker, voice message and so on still registers the contact and
  is stored. The text is the caption, or an empty string (`messages.text` allows it); a sticker's text
  is its emoji (empty without one). Media is not downloaded. Since CYR-58 the kind of media is stored
  with the message in `messages.media_kind`: `voice`, `video_note`, `photo`, `video`, `sticker` or
  `other`, empty for a plain text message (a web page preview is plain text). The userbot detects it
  from the read attributes of the Telethon message; the precedence is voice, circle (`video_note`),
  sticker, photo, video (a GIF or an animation counts as video), then any other media.
- **Idempotency.** A repeated or redelivered message creates nothing twice, keeps its original text
  and publishes nothing, because the event is published only by the call that created the contact.
- **Race.** Two simultaneous first messages from one sender both find no contact; one `create`
  raises `ContactAlreadyExists` because `tg_id` is unique. The loser reads the contact by `tg_id`
  and continues as for a known sender. The event is published exactly once. If the contact still
  cannot be read, the error is raised, not swallowed.
- **Events are best-effort.** `NewContactEvent` goes to a bounded in-memory queue (1000). A full
  queue drops the new event with a warning that carries counters only; intake never fails because
  of it. The database is the source of truth: a contact in status `new` exists whether or not its
  event was delivered or consumed. A crash between creating the contact and publishing loses the
  event, not the contact.
- **Catch-up.** On every start, after the handler is registered and the session is verified, the
  userbot asks Telethon to catch up, so messages that arrived while the app was down are processed.
  The client is built with `catch_up=True`, which makes Telethon load the update state saved in the
  session file; a fresh session has no saved state, so nothing older than its first connect is
  fetched. Replays are safe because of the idempotency above. A stop signal during catch-up ends
  the start at once.
- **Errors.** The intake call is tried up to 3 times (1 s, then 2 s between attempts) for any
  exception except cancellation; the repeat is safe because of the idempotency above. If the last
  attempt fails too, the error is logged with the error class and counters only and the message is
  dropped; the listener keeps running. A message that failed all attempts (for example during a
  longer database outage) can stay unregistered. If the contact was created and a later step then
  failed, the repeat finds the contact and no `NewContactEvent` is published for it; the contact
  stays in the database with status `new`; the startup sweep of the
  [profile collection](#profile-collection-cyr-11) finds it.
- **Who consumes the event.** The profile collector consumes `NewContactEvent` first; the card
  announcer (CYR-12) waits for the `ContactReadyEvent` that the collector publishes afterwards.

## Profile collection (CYR-11)

Exists. `ProfileCollector` in `services/profile_collector.py` runs as a background task of the app
process, started after the userbot has connected and caught up, and cancelled before the client
disconnects. It never sends and never changes a status.

- **What triggers it.** A `NewContactEvent` from the in-memory queue, and a sweep at start: before
  it consumes events, the collector processes every contact in status `new` whose profile has no
  `collected_at`. The sweep repairs lost events (a crash, a full queue, a restart) and retries
  contacts whose Telegram part failed earlier. A contact found by both the sweep and an event is
  collected once: the second look sees `collected_at` and skips it.
- **One at a time.** Events are handled strictly one after another, on purpose: it keeps the load
  on Telegram low. Every exception of one event is caught and logged with the class name and a
  counter; only cancellation stops the loop.
- **Per contact.** Skip if `collected_at` is already set (idempotent; a skipped contact publishes
  no ready event, its first run did). Otherwise four parts run in order, each in its own
  `try/except`, so one failure never affects another:

  | Part | What | Source |
  | --- | --- | --- |
  | Telegram profile | username, display name, bio | `ProfileSource.fetch_profile` |
  | Photo | the current big profile photo | `ProfileSource.download_photo` |
  | Leomatch | the card text, the match text and the name under the link to the contact, from the chat with the matching bot | `LeomatchSource.find_texts` |
  | Card photos | the photos of the card position (up to `MAX_CARD_PHOTOS`, 5), when it has any; runs only after a Leomatch lookup that found a match | `CardPhotos.download(index, destination)`, from the same lookup |

  The Leomatch lookup uses the username that the Telegram part just fetched (the stored one if that
  part failed); how it finds the card is described in [The card position](#the-card-position). The
  whole contact has a time budget of 30 s (`COLLECTION_TIMEOUT_SECONDS`,
  `asyncio.wait_for`). When it runs out, what was gathered so far is still stored.
- **Stored.** The profile JSON is written through `ContactRepository.update`; when the Telegram
  part succeeded, `ContactRepository.update_identity` first refreshes `username` and
  `display_name`, which CYR-10 wrote once from the first message. NUL bytes are removed and every
  text is truncated to a named limit (`domain/models.py`) before anything is stored.
- **`collected_at`** is set (UTC) only if the Telegram profile part succeeded. Otherwise it stays
  empty, so the next start retries the contact. A failed photo, card photos or Leomatch part does not prevent
  it and is not retried; values gathered by an earlier run are kept when a later run fails that
  part.
- **Always ready.** `ContactReadyEvent(contact_id)` is published in a `finally` for every contact
  that was attempted, also when every part failed or the budget ran out, so the card never waits
  forever. It means "profile collection finished, successfully or not, the card may be shown"; the
  consumer reads the profile and shows what is there. A contact that does not exist, or that is
  already collected, publishes nothing. Both queues are in memory and lost on restart; the card
  announcer finds cards to show after a restart in the database (its sweep).
- **Photo.** Saved as `profiles/<internal contact id>.jpg` under `MEDIA_DIR`, never named by the
  Telegram id or a name. It is downloaded to a `<id>.part.jpg` file first and renamed when the
  download is complete, so a failed download leaves no half file. The path stored in the profile is
  relative to `MEDIA_DIR`.
- **Card photos.** The same way, as `profiles/<internal contact id>-card-<n>.jpg` (n from 1, in the
  order of the message ids of the album) through `<id>-card-<n>.part.jpg`, each downloaded read-only
  from one message of the card position. The relative paths are stored in the list
  `leomatch.photos`; the Telegram profile photo stays in `photo`. A failed download is part
  `card_photos`: it leaves no half file and never blocks another photo, another part or the ready
  event. A photo whose download fails keeps the old path at its index when an earlier run stored
  one, and the stored list never shrinks because of a failed attempt, a card with fewer photos or a
  card without photos. A successful download replaces the path at its index.

### The card position

The matching bot sends a profile card as one message group: up to 3 photos with the caption on one
of them, or a single video with a caption. Telegram gives every message of such a group the same
`grouped_id`. The lookup reads the last `CARD_HISTORY_LIMIT` (15) messages before the match message,
newest first, and builds positions:

- consecutive messages with the same non-null `grouped_id` form ONE position; any other message is a
  position of its own;
- at most `CARD_WALK_BACK_POSITIONS` (2) positions are looked at, so the chains stay as before:
  (A) card, the owner's own like (outgoing), match message; (B) card, match message. An album is one
  position in both;
- outgoing positions (the like) and positions with no text or caption in any message are skipped;
- the first incoming position with text is the card, and the walk stops there: never two cards. Its
  card text is the first non-empty caption among its messages, ordered by message id;
- if any message of that position has a user-mention entity, a `tg://user` link or a `t.me/` link,
  it looks like another person's match message: the walk stops and no card is taken;
- the card photos are the messages of the chosen position that have a photo, ordered by message id,
  at most `MAX_CARD_PHOTOS` (5). A video message has no photo and is never downloaded; a card that
  is a single video gives its caption text and no photos.

Which message of an album carries the caption, and in which order Telegram returns the messages of
an album, are not observed yet (see [decisions.md](decisions.md)); the rules above do not depend on
either.

### The stored profile

`contacts.profile` is a JSONB object with this structure (`ContactProfile` in `domain/models.py`).
An empty object `{}` means nothing was collected.

| Field | Holds                                                                                                                                                                                                                                                                                                                                                                                                              |
| --- |--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `username`, `display_name`, `bio` | As fetched, sanitized and truncated; `null` when missing                                                                                                                                                                                                                                                                                                                                                           |
| `photo` | The Telegram profile photo: path relative to `MEDIA_DIR`, for example `profiles/17.jpg`, or `null`                                                                                                                                                                                                                                                                                                                                             |
| `leomatch` | `null` or an object: `raw_text` (the card text only, `null` when no card was found; the match text never goes here), `match_text` (the text of the match message), `photos` (list of card photo paths relative to `MEDIA_DIR`, for example `profiles/17-card-1.jpg`, at most 5, empty when there are none), `name`, `name_source` (`card` for a name parsed from the card text, `match` for the name under the link to the contact in the match message, `null` without a name) and, only when the card text matches the observed card format, `age` (14 to 99), `city` and `about` (`about` stays `null` when the card has no description) |
| `collected_at` | ISO 8601 UTC time, or `null` while the Telegram part has not succeeded                                                                                                                                                                                                                                                                                                                                             |

The card and match formats were observed on one real match; what is still unobserved is listed in
[decisions.md](decisions.md).

Old contacts keep their old shape and only newly collected ones get the new one; no migration. The
model reads the legacy single `leomatch.photo` key as a one-item `photos` list, ignores the legacy
`related_texts`, and treats a legacy `name` without `name_source` as a card name. A legacy
`raw_text` that was a match text (the old fallback) stays as written.

## Card announcement (CYR-12)

Exists. `CardAnnouncer` in `services/card_announcer.py` runs as a background task of the app
process next to the control bot, started after the userbot has connected and caught up and
cancelled first on shutdown. It talks to the owner only through the `CardSender` Protocol.

- **Inputs.** The `ContactReadyEvent` from the in-memory queue, and a sweep that runs once at start
  and then every `CARD_SWEEP_INTERVAL` (120 s). Both only enqueue the contact id; one consumer
  handles the ids strictly one at a time, so the owner gets cards in order and Telegram sees no
  bursts. An id that is already waiting is not queued twice.
- **Sweep rule.** A contact in status `new` is announced when its profile has `collected_at`, or when
  it is older than `CARD_WITHOUT_PROFILE_DELAY` (600 s) by `created_at`, so a collector that keeps
  failing never keeps the owner waiting. A contact that is `new`, not collected and younger than
  that waits for its ready event. The event itself needs no such rule: it means the collection
  finished, successfully or not.
- **Per contact.** Re-read the contact and skip it unless its status is still `new`. Build the
  `ContactCard`: identity, bio, the matching-bot card, the last up to three incoming messages in
  chronological order, the profile photo path and the card photo paths, whether the profile was
  collected, and the enabled modes.
  Call the sender. **Only after it returned `True`** set `new -> pending`. A `False` result or an
  exception leaves the contact in `new`; the next sweep retries it. Failures are counted, and the
  announcer logs at most one warning per sweep interval, with counters and the error class only.
- **Duplicates.** Sending first and setting the status afterwards means a crash between the two (or
  a failed status write) shows the owner the same card twice. That is tolerated: the buttons are
  idempotent (below). The opposite order would lose cards, which is worse.
- **What the owner sees.** The photos first: the profile photo, then all card photos, as ONE media
  group (a single photo uses `send_photo`), with a caption on the first item only: «Фото профиля и
  анкеты» when both kinds exist, «Фото профиля» or «Фото анкеты» otherwise. Then one text message
  with the name, nick, messages, bio and the matching-bot card, and a line when the profile was not
  collected. The card section shows the card text and the parsed fields only from `raw_text`; when
  no card text was found it is one short line saying so, and a fallback name from the match message
  is shown as the name. The match text is never shown. The
  buttons live on the text message: one "Отвечать: <mode>" per enabled mode and "Игнорировать". All
  texts from the contact are HTML-escaped. The owner must have pressed Start in the control bot once;
  until then Telegram refuses the send and the contact stays `new`.
- **Buttons.** `CardActions` (`services/card_actions.py`) is the only code that changes a status
  from a card. Reply: the contact must be `pending` (then it becomes `active` with the mode), or
  already `active` with the same mode (nothing changes, outcome `already_applied`); a mode that is
  missing or disabled gives `mode_unavailable` and no status change. Ignore: `pending` becomes
  `ignored`; an `ignored` contact gives `already_applied`. Anything else (a card pressed after the
  contact moved on, a `new` or `paused` contact, an `active` contact through Ignore or through
  another mode) gives `refused` and changes nothing; an unknown contact gives `not_found`. The
  actions run one at a time under a lock, because the repository has no compare-and-set.
- **After a press.** The bot always answers the callback query. On `applied` and `already_applied`
  it edits the card text and appends the decision, taken from the CURRENT state of the contact (so a
  stale card shows the truth), and removes the buttons. On `refused`, `not_found` and
  `mode_unavailable` it shows a short alert; if the contact is no longer `pending`, the buttons are
  removed. A card that can no longer be edited is only answered.
- **After Reply.** With `AUTOPILOT_ENABLED=true` the autopilot (CYR-17) also answers the messages that
  arrived while the contact was `pending`: the first activation leaves `reply_after` empty, and the
  sweeper finds the newest unanswered message if it is not older than 6 h. Pausing and resuming from
  the card is CYR-26; the stop-trigger notice exists (CYR-20).

## Manual intervention (CYR-21)

Exists. If the owner writes to a contact by hand from the second account, the assistant must not talk
over them: the contact is paused, its scheduled rows are cancelled and the owner gets one notice with
a Resume button ([S4](safety-rules.md)).

**Detection.** A second Telethon handler, `OutgoingHandler` in `userbot/outgoing_handler.py`, listens
for outgoing messages next to the incoming one (`IntakeService` and the incoming handler are not
touched). A pure filter lets through only a private chat with a real user and counts everything else
at DEBUG: groups, channels, bots, the service account (777000), deleted accounts, the account itself,
an event without a usable peer id. The peer is the chat, never the sender (the sender of an outgoing
message is the account itself). The handler builds an `OutgoingMessage` (peer Telegram id, message
id, text or an empty string, Telegram's send time) and calls `ManualMessageDetector.handle`. It
catches everything, retries a failed detection up to 3 times (1 s, then 2 s) and never retries a
detection that already paused the contact, because the detector never raises after the pause.

**Classification** (`services/manual_intervention.py`). A message the sender itself sent also arrives
as an outgoing event, and the assistant must not take its own echo for the owner:

| Step | What happens |
| --- | --- |
| 1 | The contact is read by the peer's Telegram id. Unknown peer: nothing happens, nothing is stored |
| 2 | Whether the contact has a row in state `sending` is read **first**, then whether the message id is already stored. Stored: it is the sender's own message (or an event seen before), ignored |
| 3 | If it is not stored but a row is `sending`, the echo may have arrived before the sender stored its message id. The detector re-checks the message id after each of the delays 0.25, 0.5, 1, 2, 4 and 8 s (15.75 s in all, injected sleep, early exit as soon as the id appears). Stored by then: own message, ignored |
| 4 | Otherwise the message is manual. The status is re-read now, because it may have changed during the wait |
| 5 | Only an `active` contact is paused: `set_status(paused, pause_reason=manual_message)`. Any other status is left as it is (an `ignored` contact stays ignored, an earlier pause keeps its first reason) and the message is only stored |
| 6 | If the status really changed (the `changed` flag), the contact's scheduled rows are cancelled with the reason `manual_message`, the message is stored with its resolved time ([Message times](#message-times); a missing or naive time does not stop the pause), and the owner gets ONE notice. Each of these three steps is guarded on its own: a failure is logged with the step and the error class and never undoes the pause, never blocks the other steps and never reaches the handler |

The order is pause first, then cancel, store, notify. Storing first and pausing afterwards would let a
replayed event (a retry, a catch-up after a restart) find the message already stored, take it for
the sender's own and skip the pause. The same event twice therefore pauses once, stores once and
notifies once.

**The stored message.** An owner message is appended as `outgoing` with Telegram's send time as
`created_at` and does not move `contacts.last_message_at`, which stays the time of the latest message
from the contact. It counts as an outgoing message for the send limits ([S5](safety-rules.md)),
because Telegram counts it as well.

**A row in the middle of sending.** A `sending` row cannot be cancelled by the detector (the claim and
the Telegram call belong to the sender). Pausing the contact is enough: the sender checks the guard
a second time right before the send call and refuses the row with `contact_not_active`
([outbox-and-sender.md](outbox-and-sender.md#the-guard)). A test proves exactly that, with a manual
message arriving during the typing action of a real `OutboxSender`.

**The notice.** `ManualPauseNotifier.notify_manual_pause(contact_id)` is a single-method Protocol
implemented only by the control bot (`TelegramManualPauseNotifier`). It sends one plain text message
to the owner chat with the internal contact id and one button, «Возобновить автоответы». The notice
carries no name, nick, Telegram id or message text. The call has a 15 s deadline; a failure or a
timeout is logged with the error class and never undoes the pause. A crash between the pause and the
notice loses the notice, not the pause; the owner then finds the contact paused when they look for it.

**Resume.** The button calls `PauseActions.resume(contact_id)` through a callback with its own prefix
(`pause`), handled by the owner-only control bot. Under a lock: the contact must be `paused` and have
a mode; it becomes `active` with the mode it already had (`set_status` is called without a mode),
`pause_reason` is cleared, and `reply_after` is set to the resume time (CYR-17), so the autopilot
answers only what the contact writes after the resume. Outcomes: `applied`; `already_applied` (the contact is already `active`, a
second click); `refused` (not paused, or no mode, or the transition is not allowed); `not_found`.
The bot always answers the callback query. On `applied` and `already_applied` it edits the notice and
removes the button; otherwise it shows a short alert. A contact paused by a recipient rejection
(`send_rejected`) can be resumed the same way, but only a notice of a manual pause carries the button.

**Limits.** Activity from other devices of the second account that sends no messages is not seen, and
a message sent from another client is indistinguishable from the owner's by hand: both pause. Because
Telethon runs handlers one after another (`sequential_updates`), the settle wait can delay the
processing of other updates by up to 15.75 s when a row is `sending`.

## Stop-triggers (CYR-20)

Exists. `IntakeService` checks the text of each new incoming message of an `active` contact right
after storing it; older messages are never re-checked, and no other status is checked. A hit calls
`HandOverService.hand_over`: an `active` contact becomes `paused` with `pause_reason = stop_trigger`;
only a real status change cancels its scheduled rows (reason `stop_trigger`) and sends the owner ONE
notice with the classes, the source, the last 5 lines and three buttons: «Возобновить автоответы»
(`PauseActions.resume`, back to `active` with the same mode), «Оставить на паузе» (only edits the
notice) and «Игнорировать» (`PauseActions.ignore`, `paused` to `ignored`). A contact already paused
keeps its first reason and gets no second notice. A failing notice never undoes the pause, and the
gate never raises into the intake. Nothing is ever sent to the contact. Details:
[stop-triggers.md](stop-triggers.md).

## Media messages (CYR-58)

Exists, interim: until voice messages, circles and photos are understood (later tickets), the assistant
never answers a message it cannot see. After the stop-trigger check, `IntakeService` runs the media gate
on the new incoming message of an `active` contact: a `media_kind` of `voice`, `video_note`, `photo`,
`video` or `other` calls `HandOverService.hand_over` with the class `media`, the source `media` and the
kind. It is the same hand-over: `paused` with `pause_reason = stop_trigger`, the scheduled rows cancelled,
one notice («Контакт #N прислал …») with the three buttons; only a real change notifies, so a second
media message while paused, a redelivered one, or a photo whose caption already tripped a text trigger
notifies once. A sticker never hands over: it is stored as its emoji and handled as text. The gate runs
before the autopilot scheduler and the early read, never raises into the intake and never causes a
retry. The media is not downloaded, forwarded or processed; the pause also stops text replies until the
owner presses Resume. Details: [stop-triggers.md](stop-triggers.md#media-messages-cyr-58).

## The grammatical gender (CYR-46)

The gender changes no status. It decides one thing: whether the Reply buttons of a card are shown and
accepted. `contacts.gender` (migration `0012`, nullable, `female` or `male`) holds only the owner's
choice. The gender in force is resolved when needed: owner, then the summary, then the name guess,
then `DEFAULT_CONTACT_GENDER`, then unknown. `CardActions.reply` answers `gender_required` while it
is unknown, before any status change, so `pending -> active` still happens only by Reply with a known
gender. Ignore never needs it. A gender button works in every status; on a contact that is no longer
`new` or `pending` it only stores the choice and the card keeps just the gender row. Writers of the
column: `GenderActions` (the card) and the `contact gender` command, nothing else.

## What is stored per contact

Fixed in CYR-9. Table `contacts`:

| Column | Holds |
| --- | --- |
| `id` | Internal id; the one to use in logs |
| `tg_id` | Telegram user id, unique |
| `username`, `display_name` | Nick and name; both may be missing |
| `gender` | The owner's choice of the grammatical gender, `female`, `male` or empty (CYR-46) |
| `status` | One of the statuses above, starts as `new` |
| `mode_id` | Chosen mode, nullable, foreign key to `modes` |
| `pause_reason` | Why the contact is `paused`: `manual_message`, `send_rejected`, `owner` or `stop_trigger`; `NULL` for every other status and for contacts paused before CYR-21. A check constraint allows only these values (migration `0007`, widened by `0008`, no backfill) |
| `profile` | JSONB object: bio, photo path, matching-bot card text and card photo paths, `collected_at`. Structure: [The stored profile](#the-stored-profile) (CYR-11) |
| `summary` | JSONB object: the contact summary (CYR-25), `{}` when there is none: `version`, `updated_at`, `cursor` (the `created_at` and id of the last message it covers) and `data` (the fields of [summaries.md](summaries.md#the-schema)). Written only by the summary updater and cleared only by `summary clear` |
| `summary_failed_at` | When the last summary update failed, or `NULL`; the sweeper skips the contact for 10 minutes after it, and a successful update or `summary clear` empties it (migration `0011`, CYR-25) |
| `last_message_at` | Time of the latest message **from the contact**, in the message time of [Message times](#message-times); only moves forward, a late event or an older replayed message does not take it back. A message we send does not move it (CYR-19) |
| `reply_after` | When the owner last resumed the contact, or `NULL`; the autopilot never answers a message whose time is at or before it (migration `0009`, CYR-17), and the silence of a proactive message counts from it when it is later than the last message (CYR-23). Since the wrap-up part of CYR-23 it also moves forward, never backwards and without a status change, to the time of a message the model chose not to answer with `SKIP` shortly before a night or busy window (`ContactRepository.set_reply_after`, called only by the reply job) |
| `proactive_not_before` | The moment before which no proactive message is considered, or `NULL` (migration `0013`, CYR-23). Written only by the proactive job: now + `PROACTIVE_MIN_INTERVAL_HOURS` after it wrote a message, now + 12 h after the model answered `SKIP`, now + 1 h after a stop or a final cooldown pause. Nothing clears it; it simply passes |
| `created_at`, `updated_at` | `updated_at` moves only when a row really changes |

Message history is in table `messages` (the "last N messages" come from here), one row per Telegram
message: `contact_id`, `direction` (`incoming`, `outgoing`), `text`, `tg_message_id`, `created_at`, `media_kind`.
`media_kind` (CYR-58, migration `0014`) holds the kind of media of an incoming message or `NULL`; a check
constraint allows only the six values. `incoming` rows are written by the registration above, with Telegram's send time as `created_at`; `outgoing` rows by the sender's
`complete_send`, in one transaction with marking the outbox row sent, with the Telegram message id and
Telegram's send time (CYR-19), and, for messages the owner wrote by hand, by the manual-message
detector with the resolved send time (Telegram's, or now by the rule of [Message times](#message-times)) as `created_at` and without moving `last_message_at` (CYR-21, CYR-40).
There is no counter of unanswered proactive messages: the proactive rule counts the `proactive` rows of
the outbox after the contact's last message ([proactive.md](proactive.md#the-eligibility-rule)), and a
proactive message never changes the status (CYR-23).

All timestamps are `timestamptz` stored in UTC. All of it is personal data: it stays in the database
and the media volume, is never logged at INFO or above and never goes to git
([S9](safety-rules.md)).

## Message times

Both directions store Telegram's send time as `messages.created_at`, never the moment the row was
inserted, unless the rule below replaces a missing or implausible time with now. After downtime the userbot catches up on what arrived meanwhile, and an old message must
look old: the stale age of 6 hours, the job's age check, the sweeper, the `reply_after` comparison, the
planner's burst and ongoing-conversation rules and the history in the prompt all read this time.

- **The rule, for both directions** (`message_time` in `services/message_time.py`, CYR-40, used by
  `IntakeService` for an incoming message and by `ManualMessageDetector` for a message the owner wrote
  by hand): the time Telegram gives (`IncomingMessage.sent_at`, `OutgoingMessage.sent_at`) is stored. If
  it is more than `MAX_FUTURE_SKEW` (5 minutes, `domain/models.py`) ahead of the clock the service was
  built with, the clock's `now` is stored instead, because a clock that is wrong must not make a message
  newer than every reply. A time within the skew is kept as given. A missing time, or a naive one (both
  models turn a naive `datetime` into a missing time, and the userbot handlers pass a missing or naive
  event date on as missing instead of dropping the event), also becomes `now`. For an incoming
  message `contacts.last_message_at` is touched with the value that ended up stored; a message of the
  owner never touches it.
- **A second delivery changes nothing.** The unique `(contact_id, tg_message_id)` keeps the first
  row, so the stored time is the first one even when the second delivery would have been clamped to
  a later `now`; `last_message_at` is touched with the stored time, so it does not move either.
- **Ordering is by the pair `(created_at, id)`, never by `id` alone and never by `created_at` alone.**
  A catch-up can insert an older message with a larger `id`; `created_at` decides which is newer and
  `id` only breaks a tie (Telegram's time has one-second resolution). It applies to: the last-N
  listing (`list_last`: the history of the prompt, the planner, the job, the hand-over lines, the
  card, `stop-check`), the job's supersession and "answered" check (the last element of that listing
  against the trigger), the planner's burst (a run in that order), the sweeper's and the status
  count's "latest message of the contact" and the `outgoing_times` of the limits.
- **Rows stored before this rule keep their old times.** Nothing is migrated: an incoming row stored
  earlier has the time of its insertion and ages out by the same rules.

## Idempotency of messages

Telegram can deliver the same event more than once. `messages` has a unique constraint on
`(contact_id, tg_message_id)`, and `MessageRepository.append` (used by the intake) inserts with `ON CONFLICT DO NOTHING`:
a duplicate creates nothing and returns the row already stored, with its original text, including
when both events arrive at the same time. `list_last` returns the newest N in chronological order
(`created_at`, ties broken by `id`).

## Debounce and re-check rules

- Each incoming message of an `active` contact enqueues one job, deferred by
  `AUTOPILOT_DEBOUNCE_ONGOING_SECONDS` (4 s) in an ongoing conversation and by
  `AUTOPILOT_DEBOUNCE_SECONDS` (12 s) otherwise, by the planner's ongoing rule (CYR-43), with the id
  `reply-<contact id>-<message id>`. The debounce is by supersession: a job whose message is no longer
  the latest message of the contact skips, so a burst produces one reply (CYR-17,
  [autopilot.md](autopilot.md#the-debounce)); once the burst is `AUTOPILOT_MAX_BURST_WAIT_SECONDS`
  (30 s) old, a job answers the newest message instead of skipping
  ([autopilot.md](autopilot.md#the-maximum-wait)). The reaction delay is longer than the debounce
  window, so a job of an earlier message may already have written a row; while that row has not
  started it is cancelled as `superseded` by the newer job and by the sender's check before the read
  mark (CYR-41, [autopilot.md](autopilot.md#one-reply-per-burst)); a started reply is finished.
- The status is checked when the job is enqueued, when it runs, in `OutboxService.enqueue` and again
  right before the send. The last check is made by the sender, not by the caller, so a status change
  between scheduling, prompt building and sending (for example a stop-trigger or a manual message of
  the owner) always cancels the reply ([outbox-and-sender.md](outbox-and-sender.md#the-guard)).
- A job whose contact is not `active` when it runs finishes without sending.
- A manual message of the owner in the chat pauses the contact (CYR-21, exists); a job already
  scheduled then ends at the status re-check, and the sender's rows of that contact are cancelled
  with the reason `manual_message`.
- Reminders for `pending` cards (CYR-24) do not change the status.
