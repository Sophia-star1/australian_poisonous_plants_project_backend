# ---------- builder ----------
FROM python:3.12-slim AS builder

WORKDIR /build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

WORKDIR /app

COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build/app ./app
COPY --from=builder /build/alembic ./alembic
COPY --from=builder /build/alembic.ini ./alembic.ini
# The migrations, so `python -m app.db.migrate` runs from the image itself:
# cloudbuild.yaml applies them with this image before deploying it.
COPY --from=builder /build/sql ./sql

ENV VIRTUAL_ENV="/app/.venv" \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

USER app

EXPOSE 8000

# Use python -m to avoid venv relocation issues with entry-point scripts
CMD python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1
