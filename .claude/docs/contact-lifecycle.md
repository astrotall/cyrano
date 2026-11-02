# Contact lifecycle

Everything here is **planned** unless stated; as of CYR-8 no contact exists in the code. The
storage arrives with CYR-9 and the transition matrix is fixed there.

## Statuses

`new -> pending -> active -> paused / ignored`

| Status | Meaning | Automatic replies | Set by |
| --- | --- | --- | --- |
| `new` | Registered by the userbot from an unknown sender | Never | userbot (CYR-10) |
| `pending` | A card was sent to the owner, waiting for a decision | No | control bot flow (CYR-12) |
| `active` | The owner pressed Reply and chose a mode | Yes, via the sender only | owner (CYR-12, CYR-15) |
| `paused` | Stopped by a stop-trigger, by a manual message of the owner, by two unanswered proactive messages, or by the owner | No | system (CYR-20, CYR-21, CYR-23) or owner |
| `ignored` | The owner pressed Ignore | Never; silence forever | owner (CYR-12) |

Only `active` is allowed to receive an automatic reply. See [S1](safety-rules.md).

## Transitions

| From | To | Trigger | Who or what | Ticket |
| --- | --- | --- | --- | --- |
| (none) | `new` | An unknown sender writes to the second account | userbot | CYR-10 |
| `new` | `pending` | The profile is collected and the card is sent to the owner | userbot and control bot | CYR-11, CYR-12 |
| `pending` | `active` | Owner presses Reply and chooses a mode | owner | CYR-12, CYR-15 |
| `pending` | `ignored` | Owner presses Ignore | owner | CYR-12 |
| `active` | `paused` | A stop-trigger fires | system | CYR-20 |
| `active` | `paused` | The owner writes a manual message in the chat | system | CYR-21 |
| `active` | `paused` | Two proactive messages in a row stay unanswered | system | CYR-23 |
| `active` | `paused` | The owner pauses the contact | owner | decided in CYR-9 |
| `paused` | `active` | Owner presses Resume | owner | decided in CYR-9 |

Every other transition (for example from `ignored`, `new` to `ignored`, `paused` to `ignored`) is
decided in CYR-9, which fixes the full matrix. Changing the matrix later is a large change (see
[CLAUDE.md](../../CLAUDE.md)).

## Message flow

| Step | What happens | Ticket |
| --- | --- | --- |
| 1 | An unknown sender writes to the second account; the userbot registers a contact (`new`). No reply is sent | CYR-10, CYR-9 |
| 2 | The userbot collects the profile: nick, name, bio, photo, and the matching-bot profile text if present | CYR-11 |
| 3 | The control bot sends the owner a card with a Reply button per mode and an Ignore button; the contact becomes `pending` | CYR-12, CYR-15 |
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

## What is stored per contact (planned, CYR-9)

Indicative; the schema is fixed in CYR-9.

- Telegram identity of the contact (user id, nick, name), bio, a reference to the photo in the
  media volume, and the matching-bot profile text if any.
- Status and the chosen mode.
- Timestamps in UTC.
- Message history (the "last N messages" come from here).
- Contact summary (CYR-25).
- Counter of unanswered proactive messages (CYR-23).

All of it is personal data: it stays in the database and the media volume, is never logged at INFO
or above and never goes to git ([S9](safety-rules.md)).

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
