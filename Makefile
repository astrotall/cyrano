.PHONY: install check test fmt up down logs migrate revision db-up login login-docker

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
