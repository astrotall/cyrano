.PHONY: install check test fmt up down logs migrate revision db-up login login-docker llm-check persona-import examples-import examples-stats examples-convert reply-preview outbox-enqueue outbox-list outbox-cancel outbox-retry outbox-status outbox-resume setup-hooks

install:
	uv sync --locked

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

test:
	uv run pytest

fmt:
	uv run ruff check --fix .
	uv run ruff format .

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f

migrate:
	uv run alembic upgrade head

persona-import:
	@test -n "$(FILE)" || (echo 'usage: make persona-import FILE=data/persona.md' >&2; exit 2)
	uv run python -m cyrano persona import "$(FILE)"

examples-import:
	@test -n "$(FILE)" || (echo 'usage: make examples-import FILE=data/style-examples.json' >&2; exit 2)
	uv run python -m cyrano examples import "$(FILE)"

examples-stats:
	uv run python -m cyrano examples stats

examples-convert:
	@test -n "$$INPUT" || (echo 'usage: make examples-convert INPUT=data/result.json (ME=name | ME_ID=user123) [OUT=data/style-examples.json]' >&2; exit 2)
	@if [ -n "$$ME" ] && [ -n "$$ME_ID" ]; then echo 'give exactly one of ME and ME_ID' >&2; exit 2; fi
	@if [ -z "$$ME" ] && [ -z "$$ME_ID" ]; then echo 'give exactly one of ME and ME_ID' >&2; exit 2; fi
	uv run python -m cyrano examples convert "$$INPUT" $${ME:+--me "$$ME"} $${ME_ID:+--me-id "$$ME_ID"} --out "$${OUT:-data/style-examples.json}"

reply-preview:
	@test -n "$(CONTACT)" || (echo 'usage: make reply-preview CONTACT=<contact id> [MODE=<mode name>]' >&2; exit 2)
	uv run python -m cyrano reply-preview "$(CONTACT)" $(if $(MODE),--mode "$(MODE)")

outbox-enqueue:
	@test -n "$$CONTACT" -a -n "$$TEXT" || (echo 'usage: make outbox-enqueue CONTACT=<contact id> TEXT="<text>" [IN=<seconds>]' >&2; exit 2)
	uv run python -m cyrano outbox enqueue $${IN:+--in-seconds "$$IN"} "$$CONTACT" -- "$$TEXT"

outbox-list:
	uv run python -m cyrano outbox list $${STATE:+--state "$$STATE"} $${LIMIT:+--limit "$$LIMIT"}

outbox-cancel:
	@test -n "$$ID" || (echo 'usage: make outbox-cancel ID=<row id>' >&2; exit 2)
	uv run python -m cyrano outbox cancel "$$ID"

outbox-retry:
	@test -n "$$ID" || (echo 'usage: make outbox-retry ID=<row id> [IN=<seconds>] [YES=1]' >&2; exit 2)
	uv run python -m cyrano outbox retry "$$ID" $${IN:+--in-seconds "$$IN"} $${YES:+--yes}

outbox-status:
	uv run python -m cyrano outbox status

outbox-resume:
	uv run python -m cyrano outbox resume $${YES:+--yes}

revision:
	@test -n "$(m)" || (echo 'usage: make revision m="what changed"' >&2; exit 2)
	uv run alembic revision --autogenerate -m "$(m)"
	uv run ruff check --fix --quiet migrations
	uv run ruff format --quiet migrations

db-up:
	docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --wait postgres

login:
	uv run python -m cyrano login

login-docker:
	docker compose run --rm app python -m cyrano login

llm-check:
	uv run python -m cyrano llm-check

setup-hooks:
	git config core.hooksPath .githooks
