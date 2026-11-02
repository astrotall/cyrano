# Safety rules

**All rules below are planned until their tickets land.** As of CYR-8 nothing sends messages, so
none of them is enforced by code yet; they are the contract that the tickets must implement and
test. Changing or dropping a rule is a large change (see [CLAUDE.md](../../CLAUDE.md)) and ships
with the code and the test ([CONTRIBUTING.md](../../CONTRIBUTING.md)). Statuses:
[contact-lifecycle.md](contact-lifecycle.md).

Tests named here are planned. They use scripted fakes and fake data only.

## S1. Only active contacts get replies

- **Rule:** an automatic reply is sent only to a contact in status `active`. The status is checked
  when the reply is scheduled and again right before it is sent.
- **Why:** replying to someone the owner has not approved is the worst failure of the product.
- **Enforced where:** the status guard inside the sender (CYR-19); the scheduling check in the
  worker job (CYR-17, CYR-19).
- **How it is tested:** every status other than `active` gets no send; status changed from
  `active` to `paused` between scheduling and sending gets no send; contact missing at send time.

## S2. One sender is the only way out

- **Rule:** every outgoing message goes through the sender. No other code path sends. This
  includes replies, proactive messages and reminders to contacts.
- **Why:** guards, limits and pacing can only be trusted if they cannot be bypassed.
- **Enforced where:** the sender in `services` (guard CYR-19, humanization CYR-18, limits CYR-22);
  the import rules in [architecture.md](architecture.md); review.
- **How it is tested:** each guard has its own tests through the sender; a check that no Telethon
  send call exists outside the sender is planned once the userbot lands (CYR-10).

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
- **Enforced where:** a check on every update in the control bot (CYR-12). The setting itself
  already exists in `config.py`; the check does not.
- **How it is tested:** an update from another user id gets no answer and no side effect; a
  callback from another user does not change a status.

## S9. No personal data in logs or git

- **Rule:** message text, names and other personal data are never logged at INFO or above. Persona
  text, style examples, sessions, media and messages never go to git; fixtures use fake data.
- **Why:** the data belongs to real people.
- **Enforced where:** exists today: `.gitignore` and `.dockerignore` exclude `.env`, `*.session`,
  `media/` and `data/`; secrets are `SecretStr` and masked in logs; `Settings.safe_summary()`, the
  only settings in the startup log lines, returns an allowlist (`LOGGED_SETTINGS`) with no secrets,
  no `OWNER_TELEGRAM_ID` and no `TELEGRAM_API_ID`. Planned: scrubbing of free-text
  logs and observability (CYR-28); until then, review enforces it (hard rules 8 and 9).
- **How it is tested:** exists today: tests assert that neither secret values nor the owner id and
  API id appear in the app and worker startup output or in `safe_summary()`, and that every
  setting is either allowlisted or deliberately withheld. Planned: tests that run the reply pipeline with a fake message and assert that its text
  does not appear in captured log output at INFO and above.

## S10. The assistant never answers a direct bot question

- **Rule:** when a contact asks directly whether it is a bot or AI, the assistant does not answer.
  It stops the conversation and notifies the owner. This is a stop-trigger (S3).
- **Why:** a wrong or evasive answer is worse than silence, and the owner decides what to say.
- **Enforced where:** stop-triggers (CYR-20).
- **How it is tested:** direct questions in different phrasings stop the conversation, no reply is
  generated, the contact is `paused` and the owner is notified.
