# Contact lifecycle

The storage and the transition rules exist (CYR-9), and so does the registration of unknown senders
(CYR-10): the userbot creates contacts with status `new` and stores their incoming messages. The
collection of the profile for the owner's card exists too (CYR-11): it fills `profile` and changes
no status. The other flows that drive the lifecycle are **planned**, each marked with its ticket;
nothing changes a status yet.

## Statuses

`new -> pending -> active -> paused / ignored`

| Status | Meaning | Automatic replies | Set by |
| --- | --- | --- | --- |
| `new` | Registered by the userbot from an unknown sender | Never | userbot (CYR-10, exists) |
| `pending` | A card was sent to the owner, waiting for a decision | No | control bot flow (CYR-12) |
| `active` | The owner pressed Reply and chose a mode | Yes, via the sender only | owner (CYR-12, CYR-15) |
| `paused` | Stopped by a stop-trigger, by a manual message of the owner, by two unanswered proactive messages, or by the owner | No | system (CYR-20, CYR-21, CYR-23) or owner |
| `ignored` | The owner pressed Ignore | Never; silence forever | owner (CYR-12) |

Only `active` is allowed to receive an automatic reply. See [S1](safety-rules.md).

## Transitions

**Decided in CYR-9.** The rules exist as data plus one function in
`src/cyrano/domain/transitions.py`; `ContactRepository.set_status` enforces them under a row lock.
The triggers and their tickets below are planned. Changing the matrix is a large change (see
[CLAUDE.md](../../CLAUDE.md)).

| From | To | Trigger | Who or what | Trigger ticket |
| --- | --- | --- | --- | --- |
| (none) | `new` | An unknown sender writes to the second account | userbot | CYR-10 (exists) |
| `new` | `pending` | The profile is collected and the card is sent to the owner | userbot and control bot | CYR-11, CYR-12 |
| `pending` | `active` | Owner presses Reply and chooses a mode | owner | CYR-12, CYR-15 |
| `pending` | `ignored` | Owner presses Ignore | owner | CYR-12 |
| `active` | `paused` | A stop-trigger fires | system | CYR-20 |
| `active` | `paused` | The owner writes a manual message in the chat | system | CYR-21 |
| `active` | `paused` | Two proactive messages in a row stay unanswered | system | CYR-23 |
| `active` | `paused` | The owner pauses the contact | owner | none yet |
| `active` | `ignored` | The owner ignores the contact | owner | none yet |
| `paused` | `active` | Owner presses Resume | owner | none yet |
| `paused` | `ignored` | The owner ignores the contact | owner | none yet |

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

A contact may keep a mode that is later disabled. The mode is resolved when it is used:
`ModeRepository.resolve_by_id` and `resolve_by_name` return the requested mode if it is enabled and
otherwise the `default` mode. If `default` itself is missing or disabled they raise
`DefaultModeUnavailable`, so the caller stops instead of guessing a style.

## Message flow

| Step | What happens | Ticket |
| --- | --- | --- |
| 1 | An unknown sender writes to the second account; the userbot registers a contact (`new`) and stores the message. No reply is sent. Exists, see [Registration](#registration-cyr-10) | CYR-10, CYR-9 |
| 2 | The profile collector gathers nick, name, bio, photo and the matching-bot profile text if present, stores them in `profile` and publishes a `ContactReadyEvent`. Exists, see [Profile collection](#profile-collection-cyr-11) | CYR-11 |
| 3 | The control bot sends the owner a card with a Reply button per mode and an Ignore button; the contact becomes `pending`. It starts from the `ContactReadyEvent` | CYR-12, CYR-15 |
| 4 | On Reply the contact becomes `active` with the chosen mode; on Ignore it becomes `ignored` | CYR-12 |
| 5 | Each incoming message from an `active` contact schedules one delayed Arq job; a burst of messages is debounced into one job | CYR-17, CYR-19 |
| 6 | When the job runs it re-checks the status; anything but `active` ends the job with no reply | CYR-19 |
| 7 | Stop-triggers: on a hit the contact becomes `paused`, the owner is notified and no reply is sent | CYR-20 |
| 8 | The layered prompt is built: persona, mode fragment with few-shot examples, contact summary, last N messages | CYR-14, CYR-15, CYR-16, CYR-25 |
| 9 | The LLM is called through the LLM interface; the response is validated | CYR-13 |
| 10 | The reply goes through the sender: status guard, rate limits and FloodWait handling, humanized delay, typing action, splitting, quiet hours | CYR-19, CYR-22, CYR-18 |
| 11 | Background: contact summaries, proactive messages, reminders for undecided cards | CYR-25, CYR-23, CYR-24 |

How stop-triggers are detected (lists, model output, both) and at which step each one is
evaluated is an open question in [decisions.md](decisions.md). Low model confidence can only be
known after step 9.

## Registration (CYR-10)

Exists. The userbot listens on the second account and passes every real private message to
`IntakeService.handle` in `services/intake.py`. It only listens and stores: it never sends, never
marks anything as read and never answers anyone ([S2](safety-rules.md)).

What counts as a message to register: an incoming message in a private chat with a real user.
Rejected without any effect, only counted in a DEBUG log: own (outgoing) messages, groups,
channels, anything that is not a private user chat, bots, deleted accounts, the Telegram service
account (id 777000), the account itself, an event whose sender cannot be read.

Steps of `IntakeService.handle`:

| Step | What happens |
| --- | --- |
| 1 | NUL bytes are removed from the text, the username and the display name. PostgreSQL rejects them in text and JSONB, so this happens before any repository call |
| 2 | The contact is read by `tg_id`. If it is missing, it is created with status `new`; there is no transition to `pending` (CYR-12) |
| 3 | The message is appended as `incoming`; the unique `(contact_id, tg_message_id)` makes a redelivery a no-op |
| 4 | `last_message_at` is touched; it only moves forward |
| 5 | Only if this call created the contact: a `NewContactEvent` (contact id, message id) is published |

- **What is stored.** On creation the contact gets `tg_id`, `username` and `display_name` (first and
  last name joined) from the first message; both may be missing. Later messages do not update them;
  the profile collection (CYR-11) refreshes them once from Telegram. A message from a known contact is stored whatever its
  status is (`pending`, `active`, `paused`, `ignored`); only registration is stored here, nothing
  decides about a reply.
- **Media-only messages.** A photo, sticker, voice message and so on still registers the contact and
  is stored. The text is the caption, or an empty string (`messages.text` allows it). Media is not
  downloaded.
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
  (CYR-12) waits for the `ContactReadyEvent` that the collector publishes afterwards.

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
  no ready event, its first run did). Otherwise three parts run in order, each in its own
  `try/except`, so one failure never affects another:

  | Part | What | Source |
  | --- | --- | --- |
  | Telegram profile | username, display name, bio | `ProfileSource.fetch_profile` |
  | Photo | the current big profile photo | `ProfileSource.download_photo` |
  | Leomatch | the card text and the match text from the chat with the matching bot | `LeomatchSource.find_texts` |
  | Card photo | the photo of the card message, when it has one; runs only after a Leomatch lookup that found texts | `CardPhoto.download`, from the same lookup |

  The Leomatch lookup uses the username that the Telegram part just fetched (the stored one if that
  part failed). The whole contact has a time budget of 30 s (`COLLECTION_TIMEOUT_SECONDS`,
  `asyncio.wait_for`). When it runs out, what was gathered so far is still stored.
- **Stored.** The profile JSON is written through `ContactRepository.update`; when the Telegram
  part succeeded, `ContactRepository.update_identity` first refreshes `username` and
  `display_name`, which CYR-10 wrote once from the first message. NUL bytes are removed and every
  text is truncated to a named limit (`domain/models.py`) before anything is stored.
- **`collected_at`** is set (UTC) only if the Telegram profile part succeeded. Otherwise it stays
  empty, so the next start retries the contact. A failed photo, card photo or Leomatch part does not prevent
  it and is not retried; values gathered by an earlier run are kept when a later run fails that
  part.
- **Always ready.** `ContactReadyEvent(contact_id)` is published in a `finally` for every contact
  that was attempted, also when every part failed or the budget ran out, so the card never waits
  forever. It means "profile collection finished, successfully or not, the card may be shown"; the
  consumer reads the profile and shows what is there. A contact that does not exist, or that is
  already collected, publishes nothing. Both queues are in memory and lost on restart; CYR-12 finds
  cards to show after a restart in the database.
- **Photo.** Saved as `profiles/<internal contact id>.jpg` under `MEDIA_DIR`, never named by the
  Telegram id or a name. It is downloaded to a `<id>.part.jpg` file first and renamed when the
  download is complete, so a failed download leaves no half file. The path stored in the profile is
  relative to `MEDIA_DIR`.
- **Card photo.** The same way, as `profiles/<internal contact id>-card.jpg` through
  `<id>-card.part.jpg`, downloaded read-only from the card message. Its relative path is stored in
  `leomatch.photo`; the Telegram profile photo stays in `photo`. A failed download is its own part
  (`card_photo`): it leaves no half file, never blocks or changes another part and keeps
  `leomatch.photo` as an earlier run stored it. A new successful download replaces the path.

### The stored profile

`contacts.profile` is a JSONB object with this structure (`ContactProfile` in `domain/models.py`).
An empty object `{}` means nothing was collected.

| Field | Holds |
| --- | --- |
| `username`, `display_name`, `bio` | As fetched, sanitized and truncated; `null` when missing |
| `photo` | Path relative to `MEDIA_DIR`, for example `profiles/17.jpg`, or `null` |
| `leomatch` | `null` or an object: `raw_text` (the card text if a card was found, otherwise the match text), `related_texts` (the other one, at most 2), `photo` (path of the card photo relative to `MEDIA_DIR`, for example `profiles/17-card.jpg`, or `null`), and, only when the text matches the observed card format, `name`, `age` (18 to 99), `city` and `about` (`about` stays `null` when the card has no description) |
| `collected_at` | ISO 8601 UTC time, or `null` while the Telegram part has not succeeded |

The card and match formats were observed on one real match; what is still unobserved is listed in
[decisions.md](decisions.md).

## What is stored per contact

Fixed in CYR-9. Table `contacts`:

| Column | Holds |
| --- | --- |
| `id` | Internal id; the one to use in logs |
| `tg_id` | Telegram user id, unique |
| `username`, `display_name` | Nick and name; both may be missing |
| `status` | One of the statuses above, starts as `new` |
| `mode_id` | Chosen mode, nullable, foreign key to `modes` |
| `profile` | JSONB object: bio, photo path, matching-bot profile text, `collected_at`. Structure: [The stored profile](#the-stored-profile) (CYR-11) |
| `summary` | JSONB object: contact summary. Its schema arrives with CYR-25 |
| `last_message_at` | Time of the latest message; only moves forward, a late event does not take it back |
| `created_at`, `updated_at` | `updated_at` moves only when a row really changes |

Message history is in table `messages` (the "last N messages" come from here), one row per Telegram
message: `contact_id`, `direction` (`incoming`, `outgoing`), `text`, `tg_message_id`, `created_at`.
Today only `incoming` rows are written, by the registration above.
The counter of unanswered proactive messages does not exist yet; CYR-23 adds it with a migration.

All timestamps are `timestamptz` stored in UTC. All of it is personal data: it stays in the database
and the media volume, is never logged at INFO or above and never goes to git
([S9](safety-rules.md)).

## Idempotency of messages

Telegram can deliver the same event more than once. `messages` has a unique constraint on
`(contact_id, tg_message_id)`, and `MessageRepository.append` (used by the intake) inserts with `ON CONFLICT DO NOTHING`:
a duplicate creates nothing and returns the row already stored, with its original text, including
when both events arrive at the same time. `list_last` returns the newest N in chronological order
(`created_at`, ties broken by `id`).

## Debounce and re-check rules

- Each incoming message schedules one delayed job. A burst of messages must produce one reply,
  not one per message. The debounce mechanism and the window length are decided in CYR-17.
- The status is checked twice: when the job is scheduled and again right before the send. The
  second check is made by the sender, not by the caller, so a status change between scheduling,
  prompt building and sending (for example a stop-trigger or a manual message of the owner) always
  cancels the reply.
- A job whose contact is not `active` when it runs finishes without sending.
- A manual message of the owner in the chat pauses the contact (CYR-21); a job already scheduled
  then ends at the status re-check.
- Reminders for `pending` cards (CYR-24) do not change the status.
