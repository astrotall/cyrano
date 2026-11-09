# Stop-triggers

Exists since CYR-20. A stop-trigger is a topic where a wrong answer costs the owner something real.
When one fires, the assistant does not answer: the contact becomes `paused` with
`pause_reason = stop_trigger`, its scheduled rows are cancelled and the owner gets one notice with
three buttons ([S3](safety-rules.md), [S10](safety-rules.md)). Nothing is ever sent to the contact
on a hand-over.

## Where the code lives

| File | What it holds |
| --- | --- |
| `domain/enums.py` | `TriggerClass` (with `media` since CYR-58), `TriggerSource` (`text`, `model`, `media`), `MediaKind`, `HandOverOutcome`, `IgnoreOutcome`, `PauseReason.STOP_TRIGGER` |
| `domain/stop_triggers.py` | `StopDecision`, `DialogueLine`, `HandOverNotice` (with the optional `media_kind`), `MEDIA_HAND_OVER_KINDS`, the limits (4096 characters per checked text, 5 lines, 200 characters per line, 60 for the name) |
| `userbot/media_kind.py` | The read-only detection of the kind of media of a Telethon message (CYR-58) |
| `stop_trigger_patterns.py` | All pattern data: the phrases per class, the messenger and request-verb lists, the digit, handle and currency rules, the link hosts |
| `services/stop_triggers.py` | The pure detector: `normalize`, `compile_phrase`, `classes_of`, `detect`. No IO; imports only `domain` and the pattern data |
| `services/hand_over.py` | `HandOverService.hand_over(contact_id, classes, source, media_kind=None)` |
| `services/hand_over_notifier.py` | `HandOverNotifier`, a single-method Protocol (`notify_hand_over`) |
| `services/intake.py` | The text gate and the media gate on every new incoming message |
| `services/reply_postprocess.py`, `services/reply_generator.py` | The model signal (`NEEDS_OWNER`) |
| `services/reply_job.py` | The second check on the burst and the hand-over on the model signal (CYR-17) |
| `bot/notifier.py`, `bot/rendering.py`, `bot/messages.py` | `TelegramHandOverNotifier`, the notice text and keyboard (the media notice too), the Russian texts |
| `bot/handlers.py`, `services/pause_actions.py` | The three buttons: resume, keep, ignore |
| `stop_check_cli.py` | The `stop-check` dry-run command |
| `migrations/versions/0008_allow_stop_trigger_pause_reason.py` | Adds `stop_trigger` to the check constraint on `contacts.pause_reason` |

The pattern data is a top-level module, not part of `config.py`: `config` is one module with the
settings, and the data is constants, not settings (hard rule 4 allows a dedicated module).

## Classes

| Class | Value | What fires it (examples) |
| --- | --- | --- |
| Meeting | `meeting` | встретимся, увидимся, приезжай, пойдём в кино, давай на кофе, свидание, где ты живёшь |
| Contact exchange | `contact_exchange` | a phone number, an e-mail, a `@handle` of 4+ characters, a link to a profile, «дай номер», «напиши в вотсап», «какой у тебя ник» |
| Money | `money` | деньги, переведи, займи, номер карты, долг, a currency symbol, a 13 to 19 digit run (a card number) |
| Photo | `photo` | скинь фото, селфи, покажи себя, фотку, нюдсы |
| Bot question | `bot_question` | ты бот?, ты нейросеть, ты живой человек?, chatgpt, это ИИ |
| Exes | `exes` | твой бывший, мы расстались, развод, бывшая жена |
| Crisis | `crisis` | не хочу жить, покончить с собой, суицид, порезать вены |
| Media | `media` | a voice message, a circle, a photo, a video or another attachment (CYR-58). Never detected from a text: the media gate sets it, see [Media messages](#media-messages-cyr-58) |

The locked hand-over list ([decisions.md](decisions.md)) names meetings, contact exchange, money,
photos, the bot question and low model confidence. Exes and crisis are added by CYR-20 on the same
terms. Low model confidence has no numeric threshold: the model expresses it through the signal (see
[The model signal](#the-model-signal)).

## Normalisation

`normalize(text)` turns the text (cut to 4096 characters first) into several views. Every pattern
is matched against them.

1. `base`: NFKC, casefold, `ё` to `е`, every character of the categories Mn, Me and Cf removed
   (combining accents, zero-width characters, the soft hyphen, the BOM). The e-mail, handle, link,
   currency and digit rules read this view.
2. `words`: `base` with every character that is not a letter or a digit turned into a space,
   letter runs of 3 or more shortened to 2 (`бооот` to `боот`, while a legitimate double such as
   `расстались` stays), whitespace collapsed.
3. A spaced-letters view: runs of 3 or more single-letter tokens are joined (`н е й р о с е т ь`).
   Two single letters (`ты и я`) are never joined.
4. A single-letter view of each of the above, with every letter double collapsed to one (`боот` to
   `бот`). It is matched only with the alternatives that contain no double letter, so a pattern
   with `ии` can never match a lone `и`.

Digits are counted across separators: a run of digits with up to 3 separators (space, tab, newline,
the dashes, brackets) between each two digits counts as one number. 10 or more digits are a phone
(contact exchange); 13 to 19 digits are also a card (money). A short local number (`123-45-67`) is
a phone too.

## Pattern syntax

One pattern is one phrase of space-separated tokens, written in the normalised form (lower case, no
`ё`, letters and digits only):

| Token | Meaning |
| --- | --- |
| `слово` | the exact word |
| `стем*` | any word that starts with the stem |
| `а\|б\|в*` | one of the alternatives |
| `..` | a gap of 0 to 3 words |
| `..N` | a gap of 0 to N words (one digit) |
| `?слово` | an optional word |

A phrase must not start or end with a gap or an optional token and must not have an empty
alternative; `compile_phrase` refuses such a phrase with `PatternSyntaxError`, and the patterns are
compiled once at import, so a bad pattern stops the app at start-up, not at the first message.

A messenger name alone is not a trigger («я люблю телегу»). It fires only paired with a request verb
within 3 words (`CONTACT_VERBS .. CONTACT_CHANNELS` and the reverse), with a handle or with a
number.

## Where it is checked

0. **The media gate.** Exists since CYR-58, right after the intake gate below and before the autopilot
   scheduler and the early read. See [Media messages](#media-messages-cyr-58).
1. **The intake gate.** Exists. `IntakeService` runs `detect` on the text of the new incoming
   message only, right after the message is stored, and only when the contact is `active`. Older
   messages are never re-checked, so a resume does not fire again on the text that caused the
   pause. A `new`, `pending`, `paused` or `ignored` contact is not checked. The gate catches every
   error, logs it with the contact id and the error class and returns; the intake result is the
   same, and the userbot handler never retries because of the gate.
2. **The autopilot job.** Since CYR-17 the reply job runs `detect` again, right before it calls the
   model, on the burst: the run of consecutive incoming messages that ends with the message it
   answers, only messages after `contacts.reply_after`. This covers a burst whose earlier message
   fired while the gate was not checking (the contact was `pending` when it arrived, or the gate
   failed). A hit calls `HandOverService.hand_over(contact_id, classes, TriggerSource.TEXT)`, and
   nothing is generated ([autopilot.md](autopilot.md)).
3. **The model signal.** The generator recognises it, and the autopilot job acts on it. See below.

## The model signal

The reply instructions ([reply-generation.md](reply-generation.md#the-instructions)) tell the model
to reply with exactly `NEEDS_OWNER` (`NEEDS_OWNER_SIGNAL` in `prompts/texts.py`) when the last
message of the contact touches any of the classes, or when it cannot be answered without inventing
facts about the owner, and never to claim to be human or deny being an AI.

The generator checks the raw reply before cleaning. `contains_needs_owner` matches the signal in any
case, with `_`, `-`, a space or nothing between the two words, anywhere in the text. A match gives
`GeneratedReply.needs_owner = True` and an empty `text`, with the usage and the counters kept; the
reply is not cleaned and no `ReplyInvalid` is raised. A signal inside a longer reply is a leak: it
is treated the same way and logged with `needs_owner_leaked = true`. The log line carries the two
flags, never the reply.

`reply-preview` prints `needs owner (model signal)` instead of the text, then the stats line, and
exits 0. The autopilot job (CYR-17) calls `HandOverService.hand_over(contact_id, (), TriggerSource.MODEL)`
on the signal and writes no row. It hands over the same way, with the source `model` and no classes,
when a reply is truncated twice, invalid twice, or the prompt is too large.

## The hand-over

`HandOverService.hand_over(contact_id, classes, source, media_kind=None)` (the kind is only for the media
source and only goes into the notice):

| Step | What happens |
| --- | --- |
| 1 | The contact is read. Missing: `not_found`. `paused`: `already_paused`, nothing changes (an earlier pause keeps its reason). Any other status but `active`: `not_active`, nothing changes |
| 2 | `set_status(paused, pause_reason=stop_trigger)`. A refused transition is `not_active`. If the status did not really change (another writer paused it first, the `changed` flag), `already_paused` with no notice |
| 3 | One INFO line with the contact id, the class values and the source |
| 4 | The scheduled rows of the contact are cancelled with the reason `stop_trigger`. A row already `sending` is refused by the second guard of the sender because the contact is no longer `active` |
| 5 | The last 5 messages are loaded, each cut to 200 characters with `…`. A message with a `media_kind` shows its bracketed label before its caption (CYR-58, [reply-generation.md](reply-generation.md#media-labels-cyr-58)) |
| 6 | One `HandOverNotice` goes to the notifier under a 15 s deadline (`OWNER_NOTIFY_DEADLINE`) |
| 7 | `handed_over` |

Steps 4, 5 and 6 are guarded on their own: a failure is logged with the step and the error class,
never undoes the pause and never reaches the caller. A failed history load sends the notice without
lines. A crash between the pause and the notice loses the notice, not the pause.

## The notice

`TelegramHandOverNotifier` sends one HTML message to the owner chat only, with:

- the title with the internal contact id, and the display name if there is one (HTML-escaped, at
  most 60 characters);
- that the conversation is handed over and nothing was sent to the contact;
- the reason (the Russian class names, or «ассистент не может ответить сам, нужно ваше решение»
  when there are no classes: the model signal and the other model-side hand-overs of the autopilot)
  and the source (the contact's text, or «Сработало при подготовке ответа моделью.»);
- the last lines, numbered from 1, oldest first, as «Контакт» or «Вы». Every text is HTML-escaped
  and its whitespace collapsed, so a message cannot fake another numbered line; an empty text is
  «[без текста]». If the message would exceed 4096 UTF-16 units, the oldest lines are dropped;
- three buttons: «Возобновить автоответы», «Оставить на паузе», «Игнорировать».

A Telegram error is logged with the error class only.

The notice is the one place where message texts and the name leave the database for the owner
([S9](safety-rules.md)). They go to the owner's own chat through the owner-only control bot, never
to a log.

## Media messages (CYR-58)

Interim, until voice messages, circles and photos are understood (later tickets): the assistant never
answers a message it cannot see ([S3](safety-rules.md)). The media is not downloaded, forwarded or
processed; only its kind is stored (`messages.media_kind`).

**The gate order in `IntakeService.handle`.** The message is stored; then, only for an `active` contact,
(1) the text gate on the text of the new message, (2) the media gate, (3) the autopilot scheduler, (4)
the early read scheduler. The media gate runs after the text gate and before both schedulers, so the
schedulers find a paused contact and enqueue nothing. A structural test pins the order.

**The media gate.** A `media_kind` of `voice`, `video_note`, `photo`, `video` or `other` calls
`HandOverService.hand_over(contact_id, {media}, source media, kind)`. `sticker` and no kind never hand
over. It never raises into the intake (a failure is logged with the contact id and the error class and
the intake result is the same) and never makes the userbot retry. It acts only on the new message.

- **Once.** Only a real `active` to `paused` change notifies (the rule above). A second media message
  while paused, a redelivered message and an already paused contact give no notice; the gate is not even
  entered for a contact that is not `active`.
- **A caption that trips a text trigger.** The text gate runs first and hands over with its text classes;
  the media gate then finds the contact already paused (`already_paused`), so there is exactly one notice,
  the text one.
- **The pause stops text replies too** until the owner presses Resume (or ignores the contact). That is
  the price of never answering blind.
- **Stickers are text.** A sticker is stored as its emoji (empty without one) and handled like any text
  message: no hand-over, the label `[стикер]` in prompts.
- **Detection precedence** (`userbot/media_kind.py`, read attributes of the Telethon message only): a web
  page preview makes the message plain text; then `voice`, `video_note`, `sticker`, `photo`, `video` (a
  GIF or an animation counts as video), then any other media (documents, audio files, locations, contacts,
  polls, venues, dice, games, invoices, unsupported media) as `other`. It never raises: an attribute that
  fails is read as absent.

**The notice.** For the source `media` the notice reads «Контакт #N прислал голосовое сообщение» (or
«кружок», «фото», «видео», «вложение»), the name if there is one, that the assistant is paused for this
contact, that nothing was sent to it and that the media can be looked at in the chat of the second
account, the last lines (the media message as «[голосовое сообщение]», «[фото] подпись» and so on, same
escaping and truncation) and the same three buttons. It never contains a file name, the media content or
a Telegram id. `stop-check` does not report media: it is not a text class.

## The buttons

All three use the `pause` callback prefix and the owner-only middleware ([S8](safety-rules.md)).

| Button | What happens |
| --- | --- |
| Resume | `PauseActions.resume`: the contact becomes `active` with its mode and `pause_reason` is cleared (as for a manual pause) |
| Keep | Only edits the notice («оставлен на паузе») and removes the buttons. It reads and changes nothing |
| Ignore | `PauseActions.ignore`: under the same lock, a `paused` contact becomes `ignored` and `pause_reason` is cleared. `ignored` again: `already_applied`. Any other status: `refused` with an alert. Missing: `not_found` |

## The stop-check command

`python -m cyrano stop-check CONTACT_ID` or `python -m cyrano stop-check --text TEXT` (Makefile:
`make stop-check CONTACT=12` or `make stop-check TEXT="..."`, exactly one of the two). A dry run: it
writes nothing and never prints a text.

- `--text` prints `classes: meeting, photo` or `classes: none`. It needs no `.env` and no database.
  A text that starts with a dash takes the form `--text=-...`.
- `CONTACT_ID` reads the last 50 messages and prints the classes of the last 3 incoming ones, 1
  being the newest: `message 1: photo`. Without incoming messages: `no incoming message`.
- A missing contact or bad arguments give one line on stderr and exit 1.

## Adding a pattern

1. Add the phrase, in the normalised form, to the class tuple in `stop_trigger_patterns.py`.
2. Add at least one positive phrase and one close but harmless phrase to
   `tests/services/stop_trigger_phrases.py`; a phrase that fires on purpose despite being harmless
   goes to `ACCEPTED_FALSE_POSITIVES`.
3. Check by hand with `make stop-check TEXT="..."`, then run the tests. The data tests check that
   every phrase compiles, is lower case, has no `ё` and no empty alternative.

## Known false positives

The detector prefers a false hand-over to a missed one: a false positive only pauses the contact and
asks the owner. Accepted and pinned by the tests:

| Class | Fires on |
| --- | --- |
| Meeting | «она встретила парня», «я работаю в кафе», «давно не была в кино» |
| Contact exchange | «какой у тебя телефон, айфон?», «мой телефон опять сел», a list of numbers «1 2 3 … 10», «звонок будильника», «ватсап у всех есть» |
| Money | «переводчик», «тысяча причин» |
| Photo | «я фотограф», «сексуальная», «камера хранения» |
| Bot question | «ты настоящий друг», «моя программа тренировок» |
| Exes | «бывший коллега», «разошлись по домам», «разрыв шаблона» |
| Crisis | «таблетка от головы», «хочу исчезнуть на выходные» |

## Limits

- Russian and a little English only. Other languages, transliteration («vstretimsya»), homoglyphs
  (Latin letters inside Russian words) and spelled-out digits («восемь девятьсот») are not caught.
- A whole sentence in spaced letters is joined into one word and is caught only if one pattern word
  matches that joined run.
- The same Telegram message delivered twice hands over once (the second time the contact is already
  paused). A new message with the same text after a resume fires again.
- The model signal depends on the model following the instruction; the text gate is the
  deterministic part.
- After «Оставить на паузе» the notice has no buttons and there is no other way to resume the contact
  yet (CYR-26).
