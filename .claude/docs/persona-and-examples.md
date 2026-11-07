# Persona and style examples

State as of CYR-16: the persona and the style examples are imported from local files, stored in
PostgreSQL and read through services. The prompt builder reads them (see
[reply-generation.md](reply-generation.md)), but only the `reply-preview` command runs it: nothing
is wired into `main.run`.

Product and decisions: [CLAUDE.md](../../CLAUDE.md). Reasons: [decisions.md](decisions.md).

## Purpose

The assistant must write the way the owner writes. Two pieces of data make that possible, and
neither involves model training:

- the **persona**: one text that describes the owner;
- the **style examples**: short real dialogues, each ending with the owner's own reply, which the
  model later sees as few-shot examples.

## Where the data lives

The app runs in containers that cannot see the owner's files, so neither piece is read from a file
at runtime. Both are imported once by a command run locally (like `make migrate`) and stored in
PostgreSQL; the runtime reads them from the database.

| Piece | Table | Shape |
| --- | --- | --- |
| Persona | `persona` | one row, `id` fixed to 1 by a check constraint: `text`, `updated_at` |
| Style examples | `style_examples` | `id`, `tags` (JSONB list), `turns` (JSONB list), `content_hash` (unique), `created_at` |

## Privacy

The persona and the examples are personal data (S9 in [safety-rules.md](safety-rules.md)).

- The real files live only in `data/`: `data/persona.md` and `data/style-examples.json`.
  `data/` is ignored by git and by Docker.
- `.gitignore` also ignores `persona*.md` and `style-examples*.json` anywhere, so a real file saved
  by mistake in another folder is still not committed. The only exceptions are the two invented
  samples in `examples/`, the guide you are reading, and they are listed by their full names, never
  by wildcard.
- Nothing prints or logs persona or example text. Logs and messages carry counts and paths. Error
  messages of the import carry JSON-path style locations and short rules, never the text.
- `persona show` prints the length and the update time. `examples stats` prints counts per tag.

## The persona file

Plain text or Markdown, UTF-8. It must not be empty after trimming and must not exceed 8000
characters. There is no required structure. A section layout that works well:

1. **Who I am**: name, age, work, where I live.
2. **Interests**: what I do and talk about.
3. **Humor and tone**: how I write: short or long, punctuation, emoji, how I joke.
4. **Facts that must stay consistent**: things the assistant must never contradict.
5. **Things I never say**: topics, words and promises to avoid.

A sample with invented content: [examples/persona.sample.md](../../examples/persona.sample.md).

## The examples file

JSON, UTF-8:

```json
{
  "version": 1,
  "dialogues": [
    {
      "tags": ["default"],
      "turns": [
        {"role": "them", "text": "How was your week?"},
        {"role": "me", "text": "busy, but I got out on the bike on Sunday"}
      ]
    },
    {
      "turns": [
        {"role": "them", "text": "Do you cook?"},
        {"role": "me", "text": "on Sundays, yes"}
      ]
    }
  ]
}
```

A sample with invented Russian dialogues:
[examples/style-examples.sample.json](../../examples/style-examples.sample.json).

- `tags` is optional. Missing or empty means **generic**: usable by every mode. A tag is a mode
  name.
- `role` is `me` (the owner) or `them` (the contact).
- The **last turn of every dialogue is `me`**. That reply is what the model learns from; the
  earlier turns are its context.

## Validation rules and limits

The whole file is validated first. If anything is invalid, nothing is imported and up to 20
problems are printed, each as a location and a short rule, for example
`dialogues[3].turns[1].text: empty`. The text itself is never printed. The limits are constants in
`domain/style.py` and `services/style_files.py`.

| Rule | Limit or value |
| --- | --- |
| `version` | must be `1` |
| Dialogues per file | 1 to 500 |
| Turns per dialogue | 1 to 20 |
| Characters per turn | 1 to 1000 (after trimming) |
| Tags per dialogue | at most 5 |
| Tag | 1 to 32 letters, digits, underscores or hyphens; lowercased; duplicates merged |
| Roles | `me` or `them` |
| Last turn | must be `me`, which also means at least one `me` turn |
| Empty text | refused |
| Unknown keys | refused; the key is not named in the message |
| Persona length | 1 to 8000 characters |
| File size | at most 5 MiB, valid UTF-8 (a BOM is accepted) |

NUL bytes are removed from every text and every text is trimmed before it is checked and stored.
Invalid JSON, a missing file, a directory, a non-UTF-8 file and an oversized file are readable
errors too.

## Idempotency and replacing

Each dialogue has a content hash: sha256 of its normalized tags (lowercase, unique, sorted) and its
turns. Importing the same file again stores nothing new: an already stored dialogue is skipped and
counted. Importing a changed file adds only the dialogues that are new.

`--replace --yes` deletes every stored example and adds the file's dialogues inside **one
transaction**: if anything fails, the old examples stay. Without `--yes` the command refuses and
changes nothing. An empty `dialogues` list is invalid, so a replace can never wipe the examples
by accident.

Dialogues of one file get the same creation time. Their order in the file decides which counts as
newer: a later dialogue is newer.

## How examples are selected

`ExampleSelector.select(mode_name, limit)` in `services/style_examples.py`:

1. The candidates are the examples tagged with the mode (compared case-insensitively) plus the
   generic ones (no tags). An example tagged only for other modes is never a candidate.
2. Mode-tagged examples come first, then generic ones. Inside each group the newest comes first
   (`created_at` descending, then `id` descending), so the order is deterministic.
3. At most `limit` are returned. The default limit is `STYLE_EXAMPLES_MAX` (default 8, from 1 to
   50). A limit of 0, or an empty store, returns an empty list.

`PersonaService.get()` returns the persona or `None`.

The reply prompt asks for the examples of the contact's mode name and may drop some of them to fit
the budget, always from the end of this order (see [reply-generation.md](reply-generation.md)).

## From a Telegram Desktop export

State as of CYR-34: a chat exported by Telegram Desktop can be converted into a **draft** of the
examples file. The converter reads a file and writes a file. It does not touch the database, the
network or Telegram, and it needs no `.env` and no credentials. You review the draft and then import
it with the commands above.

### Export the chat

1. In Telegram Desktop open the chat with the person.
2. Open the chat menu (the three dots at the top right) and choose **Export chat history**.
3. Turn media off (photos, videos, voice messages and the rest); only the text is used.
4. Set the format to **Machine-readable JSON** (not HTML).
5. Export. Move the resulting `result.json` into `data/`.

Only a single-chat export is accepted. HTML exports are not supported; an account-wide export
(a top-level `chats` object) is refused with "export one chat".

### Find who you are in the export

Give exactly one of:

- `--me-id ID`: your `from_id`. Open `result.json`, find any message you wrote and copy its
  `from_id` value (for example `user1000000001`). This is the **recommended** way, and the only
  sensible one whenever your sender name is awkward to quote in a shell (a single punctuation
  character such as a backtick, quotes, `$`, spaces). The name does not matter in this mode; it may
  even be `null`.
- `--me NAME`: your sender name (`from`), exactly and case-sensitively.

Giving both or neither is an error. If the value matches nobody, the command stops and says only
how many distinct senders (or sender ids) it found, never their names or ids.

### The command

```sh
make examples-convert INPUT=data/result.json ME_ID=user1000000001
make examples-convert INPUT=data/result.json ME='Anna' OUT=data/style-examples.json
python -m cyrano examples convert INPUT (--me NAME | --me-id ID) [--out data/style-examples.json] \
    [--tags a,b] [--max N] [--gap-hours H] [--window K] [--redact WORD ...] [--force] [--allow-any-path]
```

The make target takes `INPUT`, exactly one of `ME` and `ME_ID`, and optionally `OUT`; values are
passed through the environment, so spaces, quotes and `$` in them are safe (a backtick is still
easier through `ME_ID`).

| Option | Meaning | Default |
| --- | --- | --- |
| `INPUT` | the `result.json` of one chat; at most 50 MiB | required |
| `--me`, `--me-id` | who you are, see above; exactly one | required |
| `--out` | the draft; must be under `data/` of the current directory | `data/style-examples.json` |
| `--tags a,b` | tags for every dialogue, validated like the import; none means generic | none |
| `--max N` | at most N dialogues, from 1 to 500; more candidates are sampled evenly | 100 |
| `--gap-hours H` | a gap longer than H hours between two messages starts a new conversation | 6 |
| `--window K` | at most K turns per dialogue, from 2 to 50 (at most 20 are kept, see below) | 6 |
| `--redact WORD ...` | extra words to replace by `<name>`, see below | none |
| `--force` | replace an existing output file | off |
| `--allow-any-path` | allow `--out` outside `data/` | off |

An error exits with 1 and a readable message, never a traceback. The output file is written
atomically (a temporary file in the same folder, validated by the real import validators, then
renamed), so a failed run leaves no partial file, and an existing output is refused without
`--force`.

### How messages become dialogues

1. Only normal messages are kept. Service messages, forwarded messages and messages with no text
   are skipped (stickers, voice and video messages, media without a caption); a media message with a
   caption is kept by its caption. A message without a usable time, or without the identity the
   chosen option needs (a name for `--me`, a `from_id` for `--me-id`), is skipped and counted.
2. Time is taken from `date_unixtime`, otherwise from `date` (a time without a zone is read as UTC,
   which only matters for the gaps). Messages are ordered by time.
3. Consecutive messages of one side merge into one **turn** (texts joined by a newline). Every
   sender except you is "them".
4. For each of your turns, the dialogue is the window of up to K turns ending with it, with your
   turns removed from the start until it begins with a "them" turn; a window without a "them" turn
   is dropped. A turn longer than 1000 characters is cut at a word boundary; a dialogue keeps at most
   the last 20 turns.
5. Exact duplicate dialogues are dropped, then, if more than `--max` remain, an evenly spaced
   deterministic sample is taken, so the same input gives the same output.

The command prints counts only: messages read and kept, skipped by reason, turns, candidates,
dialogues written, truncations, redactions by kind and the output path.

### What is redacted automatically, and what is not

Before anything else, in every kept message:

| Token | What |
| --- | --- |
| `<email>` | e-mail addresses |
| `<phone>` | a run of at least 7 digits, with `+`, spaces, dashes and parentheses between them |
| `<link>` | `http`, `https`, `www.`, `t.me/` and similar links (social sites, a bare `domain.tld/path`) |
| `<username>` | `@username` |
| `<name>` | every distinct sender name found in the export, split into words of at least 3 letters, replaced as whole words, case-insensitively (Cyrillic included); the same for every `--redact` word |

A sender name with no word of at least 3 letters (a single punctuation character, for example)
produces no pattern at all. A message that becomes only tokens is dropped.

**Not redacted**: diminutives, nicknames, other case forms of a name (a different ending is a
different word), other people's names mentioned in the text, cities, workplaces, dates and
anything intimate. Add the nicknames and forms you know with `--redact Annie Anya Ane`; a
`--redact` word is replaced as a whole word whatever its length.

### Review before you import

Read the whole draft. The converter is a heuristic: remove or edit anything that identifies a
person or says more than needed, then import it with
`make examples-import FILE=data/style-examples.json`.

**Privacy.** The examples, **including the other person's lines**, are sent to the LLM provider
inside prompts (`reply-preview` and, since CYR-17, the autopilot's reply job). Redact names and intimate details, and keep only the context that is needed: a
smaller `--window` means less of the other person's text. The export, the draft and the command
output are personal data (S9 in [safety-rules.md](safety-rules.md)): they stay under `data/`, which
git and Docker ignore, and the command never logs or prints a text, a name, an id or a number from
the chat.

## Commands

Run locally, with a complete `.env` and Postgres reachable on the host port (`make db-up`), after
`make migrate`:

| Command | What it does |
| --- | --- |
| `make persona-import FILE=data/persona.md` | `python -m cyrano persona import FILE`: validates and stores (replaces) the persona |
| `python -m cyrano persona show` | prints the persona length and update time, never the text |
| `make examples-import FILE=data/style-examples.json` | `python -m cyrano examples import FILE`: validates and adds the new dialogues |
| `python -m cyrano examples import FILE --replace --yes` | replaces all stored examples in one transaction |
| `make examples-stats` | `python -m cyrano examples stats`: total, generic count and count per tag |
| `make examples-convert INPUT=... ME_ID=...` | `python -m cyrano examples convert`: converts a Telegram Desktop export into a draft file; needs no `.env` and no database (see [From a Telegram Desktop export](#from-a-telegram-desktop-export)) |

Every command exits with 0 on success and 1 with a readable message on any error. An invalid
configuration still exits with 2 like every other command.
