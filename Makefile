.PHONY: install check test fmt up down logs migrate revision db-up login login-docker llm-check persona-import examples-import examples-stats

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
