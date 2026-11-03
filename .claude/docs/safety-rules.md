# Safety rules

**All rules below are planned until their tickets land, except where a rule says it exists.** As of
CYR-10 nothing sends messages to contacts: the userbot only listens, and a test proves it (S1 and
S2). S8 is enforced since CYR-12. The other rules are not enforced by code yet; they are the
contract that the tickets must implement and test. Changing or dropping a rule is a large change (see [CLAUDE.md](../../CLAUDE.md)) and ships
with the code and the test ([CONTRIBUTING.md](../../CONTRIBUTING.md)). Statuses:
[contact-lifecycle.md](contact-lifecycle.md).

Tests named here are planned unless a rule says they exist. They use scripted fakes and fake data
only.

## S1. Only active contacts get replies

- **Rule:** an automatic reply is sent only to a contact in status `active`. The status is checked
  when the reply is scheduled and again right before it is sent.
- **Why:** replying to someone the owner has not approved is the worst failure of the product.
- **Enforced where:** the status guard inside the sender (CYR-19); the scheduling check in the
  worker job (CYR-17, CYR-19). Exists today (CYR-10): the userbot sends nothing at all, so no
  reply can reach a contact in any status; see the structural test under S2.
- **How it is tested:** every status other than `active` gets no send; status changed from
  `active` to `paused` between scheduling and sending gets no send; contact missing at send time.
  Exists today: the structural test "the userbot never sends" (S2).

## S2. One sender is the only way out

- **Rule:** every outgoing message goes through the sender. No other code path sends. This
  includes replies, proactive messages and reminders to contacts.
- **Why:** guards, limits and pacing can only be trusted if they cannot be bypassed.
- **Enforced where:** the sender in `services` (guard CYR-19, humanization CYR-18, limits CYR-22);
  the import rules in [architecture.md](architecture.md); review. Exists today (CYR-10): the
  userbot is structurally unable to send. The client Protocol it is typed against
  (`UserbotClient` in `userbot/protocols.py`) has no write method, and Telethon is imported only
  inside `userbot/`.
- **How it is tested:** each guard has its own tests through the sender. Exists today
  (`tests/userbot/test_never_sends.py`): the members of `UserbotClient`, `IncomingEvent` and
  `SenderLike` are compared with an allowlist, so a new write method fails the test; every module of
  the userbot package is scanned for write-looking Telegram names (`send_*`, `mark_*`, `edit_*`,
  `delete_*`, `reply`, `respond` and so on), with `send_code_request` in `login.py` as the single
  allowed exception (it asks Telegram for a login code, it sends no message); and the handler tests
  run with a fake client and fake events whose write methods record the attempt and raise. When the
  sender lands (CYR-19) the scan stays and the sender's own module is the only place allowed to send.

## S3. Stop-triggers hand over

- **Rule:** meeting or date proposals, phone and contact exchange, money, photo requests, a direct
  question whether it is a bot or AI (see S10), and low model confidence stop the conversation:
  the contact becomes `paused` and the owner is notified. The assistant does not answer these
  itself.
- **Why:** these are the points where a wrong answer costs the owner something real.
- **Enforced where:** stop-trigger handling (CYR-20); trigger lists live in config or a dedicated
  module (hard rule 4); the paused status is honoured by S1.
- **How it is tested:** one case per trigger category, a message that looks close to a trigger but
  is not, invalid LLM JSON, and confidence below the threshold.

## S4. A manual message of the owner pauses the contact

- **Rule:** if the owner writes in the chat by hand from the second account, the contact becomes
  `paused`.
- **Why:** the owner has taken over; the assistant must not talk over them.
- **Enforced where:** auto-pause on manual messages (CYR-21).
- **How it is tested:** an outgoing message that was not sent by the sender pauses the contact; a
  message sent by the sender does not; a job scheduled before the manual message sends nothing.

## S5. Rate limits and FloodWait

- **Rule:** sending is rate limited centrally, and Telegram's FloodWait is handled in the sender
  by waiting or giving up cleanly, never by hammering.
- **Why:** account limits and bans are a main risk.
- **Enforced where:** the sender (CYR-22).
- **How it is tested:** limit reached delays or refuses the send; a FloodWait error from the fake
  Telegram client is honoured; no send happens during the wait.

## S6. Humanized pacing and quiet hours

- **Rule:** replies are delayed, show a typing action, are split into several messages when long,
  and are not sent during quiet hours.
- **Why:** instant, uniform replies at any hour look automated and draw limits.
- **Enforced where:** the sender (CYR-18). Quiet hours use `TIMEZONE`.
- **How it is tested:** delay and splitting with an injected clock and random source; a reply due
  in quiet hours is deferred, not dropped.

## S7. Limits on proactive messages

- **Rule:** proactive messages are limited. After two unanswered proactive messages the contact
  becomes `paused`. They go through the sender like every other message.
- **Why:** unsolicited messages are the fastest way to complaints and bans.
- **Enforced where:** proactive messages (CYR-23), the sender (CYR-22). The remaining limits are
  open questions in [decisions.md](decisions.md).
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
  exception class only. Its log lines carry the step, the model, token counts, the duration and
  retry counters. It never logs the API key.
  The persona text and the style examples are personal data too (CYR-14): they are never logged
  at any level and never go to git. The import commands and the services print and log counts,
  paths and JSON-path style locations of problems, never a persona or example text; `persona show`
  prints the length and the update time only and `examples stats` prints counts per tag. The real
  files live only in `data/`.
  A Telegram Desktop chat export is personal data too (CYR-34): it is converted locally by a
  command that reads a file and writes a file, never logs or prints a message text, a name, a
  sender id or a number (only counts), and writes the draft only under `data/` unless the owner
  passes `--allow-any-path`.
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
  failures included. Planned: tests that run the reply pipeline with a fake message and assert that its text
  does not appear in captured log output at INFO and above. Exists for the LLM client
  (`tests/llm/test_logging.py`, `tests/llm/test_complete_json.py`): all log output is captured at
  DEBUG during retries, invalid JSON replies and failures, and the prompt, the reply, the response
  body and the key are asserted absent; the raw invalid reply is also absent from the raised error.
  Exists for the persona and the examples (CYR-14): validation errors carry no input values, every
  file error is asserted to contain no marker text, the commands' output is asserted to contain no
  sample text, and a full import of the sample files with all loggers at DEBUG must leave no sample
  text in the captured logs or in the raised errors; the `.gitignore` and `.dockerignore` content
  is tested too. Exists for the converter (CYR-34): the captured output, errors and logs of a full
  run and of the failures contain none of the invented names, ids, texts or numbers, and the
  written draft contains none of the redacted strings.

## S10. The assistant never answers a direct bot question

- **Rule:** when a contact asks directly whether it is a bot or AI, the assistant does not answer.
  It stops the conversation and notifies the owner. This is a stop-trigger (S3).
- **Why:** a wrong or evasive answer is worse than silence, and the owner decides what to say.
- **Enforced where:** stop-triggers (CYR-20).
- **How it is tested:** direct questions in different phrasings stop the conversation, no reply is
  generated, the contact is `paused` and the owner is notified.
