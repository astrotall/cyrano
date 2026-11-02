# Contributing

Rules for branches, commits, Jira and pull requests. Read [CLAUDE.md](CLAUDE.md) for the
architecture decisions and hard rules.

## Who runs git

The repository owner. Claude working in this repository does not run git commands: it changes
files in the working tree and reports; the owner reviews, branches, commits and pushes.

## Branches

Format: `<type>/CYR-<number>/<short-dashed-description>`

- `type` is a commit type (table below).
- The key is uppercase: `CYR-7`, never `cyr-7`.
- The description is Latin, lowercase, 2-5 words, dashed.

Examples:

```
feat/CYR-10/telethon-userbot
fix/CYR-22/floodwait-retry
docs/CYR-7/project-documentation
chore/CYR-29/local-git-hooks
```

Rules:

- `main` is releasable. Nobody pushes to it directly; every change arrives through a pull request.
- Branches are short-lived and deleted after merge.
- One ticket, one branch, one pull request.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>): <subject>`

- Subject: English, imperative mood, up to about 72 characters, no trailing period.
- Commits are short: the first line plus the ticket key in the footer. Add a body only when the
  reason is not obvious from the subject.
- The footer is `Refs: CYR-<number>`.

Examples:

```
feat(userbot): register unknown senders as new contacts

Refs: CYR-10
```

```
feat(safety): stop and notify the owner on stop-triggers

Refs: CYR-20
```

```
fix(safety): recheck contact status right before sending

A contact paused after the job was scheduled still received a reply.

Refs: CYR-19
```

```
docs(project): add CLAUDE.md, CONTRIBUTING.md and .claude/docs

Refs: CYR-7
```

### Types

| Type | Use for |
| --- | --- |
| `feat` | New behaviour |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `refactor` | Code change that does not change behaviour |
| `perf` | Performance improvement |
| `test` | Tests only |
| `build` | Build system, Dockerfile, dependencies of the build |
| `ci` | CI configuration |
| `chore` | Maintenance that fits nowhere else |
| `revert` | Reverts an earlier commit |

### Scopes

| Scope | Area |
| --- | --- |
| `app` | Entrypoint, process lifecycle, config, logging, health |
| `db` | Models, sessions, migrations |
| `userbot` | Telethon userbot |
| `bot` | aiogram control bot, contact card |
| `llm` | LLM client |
| `persona` | Persona text, style examples, import |
| `modes` | Conversation modes |
| `prompt` | Layered prompt builder |
| `autopilot` | Delayed replies, debounce, worker jobs |
| `safety` | Sender guard, stop-triggers, rate limits, humanization, auto-pause |
| `memory` | Contact summaries, message history |
| `ops` | Docker, Compose, deployment, observability |
| `deps` | Dependencies |
| `ci` | GitHub Actions, Dependabot |
| `docs` | Documentation |
| `project` | Whole-project files and conventions |

### Breaking changes

Mark them with `!` after the scope and explain in a footer:

```
feat(app)!: rename DEEPSEEK_MODEL to LLM_MODEL

BREAKING CHANGE: update .env before upgrading.
Refs: CYR-13
```

The examples in this file show the format; they do not describe decisions.

## Jira

Project key `CYR`, always uppercase. Site: `https://astrotall.atlassian.net`.

The key appears in three places:

| Place | Form |
| --- | --- |
| Branch | `docs/CYR-7/project-documentation` |
| Commit footer | `Refs: CYR-7` |
| Pull request title | ends with the key in parentheses: `docs(project): add project documentation (CYR-7)` |

Status moves:

| Status | When |
| --- | --- |
| In Progress | Work on the ticket starts |
| In Review | The pull request is open |
| Done | The pull request is merged |
| Blocked | Work cannot continue; leave a comment with the reason and what unblocks it |

Status names are the defaults of a Jira project; if the board uses other names, map them to these
four moments.

Findings outside the task:

- A small addition that belongs to another ticket goes as a comment on that ticket.
- A bigger one becomes a new ticket.
- Neither is fixed in the current branch.

## What must ship in one commit

| Change | Ships together |
| --- | --- |
| A status transition or a safety rule | [contact-lifecycle.md](.claude/docs/contact-lifecycle.md) or [safety-rules.md](.claude/docs/safety-rules.md), the code and the test |
| A layer or an import rule | [architecture.md](.claude/docs/architecture.md) |
| A decision on an open question | [decisions.md](.claude/docs/decisions.md) |
| A new setting | The settings model (`src/cyrano/config.py`) and `.env.example` |
| A dependency | `pyproject.toml` and `uv.lock` |

## Pull requests

Before opening one, run `make check` and `make test` and make sure both pass.

- Title: the commit subject plus the key: `feat(userbot): register unknown senders (CYR-10)`.
- Body sections: Summary, Verified, Checks. End with the Jira link.

```markdown
## Summary
What changed and why, in a few lines.

## Verified
What was run or tried by hand, and what was seen.

## Checks
- make check: passed
- make test: passed

Closes https://astrotall.atlassian.net/browse/CYR-10
```

Do not merge a pull request with red CI.

## Git hooks

Planned, CYR-29. Nothing is installed today; do not assume any hook runs.

The intent is to run the same checks locally that CI runs, and to reject commit messages and
branch names that break the formats above. The exact set of hooks and the tool that manages them
are decided in CYR-29.

## Checks and CI

Locally:

```sh
make install   # uv sync --locked
make check     # ruff check, ruff format --check, mypy
make test      # pytest
make fmt       # auto-fix lint and formatting
```

CI (`.github/workflows/ci.yml`) runs on every push and pull request with Python 3.14 installed
through uv from `.python-version`:

| Job | Steps |
| --- | --- |
| `check` | `make install`, `make check`, `make test` |
| `docker` | `cp .env.example .env`, `docker compose config --quiet` with and without the override file, `docker compose build` |

Red CI is never merged.
