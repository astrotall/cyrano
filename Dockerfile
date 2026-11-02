# syntax=docker/dockerfile:1

# ---- builder: resolve and install locked deps into /app/.venv --------------------------------
FROM python:3.14-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.4 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so this layer is cached until the lock file changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- runtime: slim image, no uv, non-root ----------------------------------------------------
FROM python:3.14-slim-bookworm AS runtime

RUN groupadd --system --gid 10001 cyrano \
    && useradd --system --uid 10001 --gid cyrano --home-dir /app --shell /usr/sbin/nologin cyrano \
    && mkdir -p /data/session /data/media \
    && chown -R cyrano:cyrano /data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --from=builder --chown=cyrano:cyrano /app/.venv /app/.venv
COPY --chown=cyrano:cyrano alembic.ini ./
COPY --chown=cyrano:cyrano migrations ./migrations

USER cyrano
CMD ["python", "-m", "cyrano"]
