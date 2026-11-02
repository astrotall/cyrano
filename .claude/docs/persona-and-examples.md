# Persona and style examples

State as of CYR-14: the persona and the style examples are imported from local files, stored in
PostgreSQL and read through services. Nothing uses them yet: no prompt is built, no LLM is called
and nothing is wired into `main.run`. The prompt builder (CYR-16) will consume the selectors
described here.

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

Every command exits with 0 on success and 1 with a readable message on any error. An invalid
configuration still exits with 2 like every other command.
