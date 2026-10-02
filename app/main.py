"""FastAPI application entry point."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.config import settings
from app.core.exceptions import (
    AppException,
    app_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from app.core.logging import get_logger, setup_logging
from app.db import session as db_session
from app.middleware.cors_middleware import add_cors_middleware
from app.middleware.logging_middleware import RequestLoggingMiddleware

logger = get_logger(__name__)

#: How long `/health/ready` waits for the database. A cold process opens its
#: first connection here — TLS, auth and asyncpg's setup queries to the pooler
#: in Tokyo — and that alone measured over 5 s from outside the region, so 5 s
#: reported a healthy database as down. A hung database is still caught.
READINESS_TIMEOUT_SECONDS = 10.0

#: Values `SECRET_KEY` takes when nobody has set it: the default in
#: `config.py`, the one in `.env.example`, and nothing at all.
_PLACEHOLDER_SECRETS = frozenset({"", "change-me", "change-me-to-a-random-secret"})


def refuse_placeholder_secret() -> None:
    """Stop a production server from starting with a known signing key.

    `SECRET_KEY` signs every JWT, and its default is in this repository. A
    deployment that lost the secret binding would otherwise boot and accept
    tokens anyone can mint, for any user id — an administrator's included,
    since the role is read from that account. Refusing to start turns this
    into a failed deploy: Cloud Run keeps serving the previous revision.

    Skipped under DEBUG, so a fresh checkout still runs without a `.env`.
    Checked at startup rather than when settings load, so importing the app —
    as the test suite does — never depends on it.
    """
    if settings.DEBUG:
        return
    if settings.SECRET_KEY.strip() in _PLACEHOLDER_SECRETS:
        raise RuntimeError(
            "SECRET_KEY is unset or still a placeholder, so any token signed with "
            "it can be forged. Set SECRET_KEY to a long random value, or DEBUG=true "
            "for local development."
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown lifecycle."""
    refuse_placeholder_secret()

    if settings.redis_enabled:
        pass  # TODO: initialize Redis connection pool

    if settings.storage_enabled:
        pass  # TODO: initialize S3 client

    yield

    if settings.redis_enabled:
        pass  # TODO: close Redis connection pool


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    # First, not in `lifespan`: the CORS setup below logs at import time, and
    # structlog caches a logger on first use, so anything that logs before
    # this runs keeps the default console renderer for the life of the process.
    setup_logging()

    application = FastAPI(
        title=settings.APP_NAME,
        debug=settings.DEBUG,
        lifespan=lifespan,
    )

    add_cors_middleware(application)
    application.add_middleware(RequestLoggingMiddleware)

    application.add_exception_handler(AppException, app_exception_handler)  # type: ignore[arg-type]
    # RequestValidationError, not the bare 422 status: FastAPI raises the
    # former for body/query validation and it otherwise bypasses the envelope.
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    application.add_exception_handler(422, validation_exception_handler)
    # Last resort: keeps the {code, message, data} envelope intact for bugs.
    application.add_exception_handler(Exception, unhandled_exception_handler)

    application.include_router(api_router, prefix="/api/v1")

    @application.get("/health")
    async def health_check() -> dict[str, str]:
        """Liveness: the process is up and serving. Touches nothing else.

        Deliberately blind to the database. Wired to a liveness probe, a
        check that failed with the database would have Cloud Run restart
        healthy instances every time the database blipped — which fixes
        nothing and drops every request in flight. `/health/ready` is the
        one that looks.
        """
        return {"status": "ok"}

    @application.get("/health/ready", response_model=None)
    async def readiness_check() -> JSONResponse | dict[str, str]:
        """Readiness: whether requests that need the database can succeed.

        `/health` answered 200 for the whole month the database was down
        (2026-08-28 to 2026-09-25), so nothing watching it could tell. This
        runs `SELECT 1` and answers 503 when that fails or takes longer than
        `READINESS_TIMEOUT_SECONDS`. The cause is logged, never returned:
        this endpoint is public.
        """
        try:
            await asyncio.wait_for(db_session.ping(), timeout=READINESS_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning("readiness_check_failed", error=f"{type(exc).__name__}: {exc}")
            return JSONResponse(
                status_code=503, content={"status": "unavailable", "database": "unreachable"}
            )
        return {"status": "ok", "database": "ok"}

    return application


app: FastAPI = create_app()
