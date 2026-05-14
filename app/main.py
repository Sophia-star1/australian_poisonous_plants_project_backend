"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.router import api_router
from app.config import settings
from app.core.exceptions import AppException, app_exception_handler, validation_exception_handler
from app.core.logging import setup_logging
from app.middleware.cors_middleware import add_cors_middleware
from app.middleware.logging_middleware import RequestLoggingMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown lifecycle."""
    setup_logging()
    # TODO: initialize Redis connection pool
    # TODO: initialize S3 client
    yield
    # TODO: close Redis connection pool
    # TODO: close S3 client


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    application = FastAPI(
        title=settings.APP_NAME,
        debug=settings.DEBUG,
        lifespan=lifespan,
    )

    add_cors_middleware(application)
    application.add_middleware(RequestLoggingMiddleware)

    application.add_exception_handler(AppException, app_exception_handler)  # type: ignore[arg-type]
    application.add_exception_handler(422, validation_exception_handler)  # type: ignore[arg-type]

    application.include_router(api_router, prefix="/api/v1")

    @application.get("/health")
    async def health_check() -> dict[str, str]:
        """Return service health status."""
        return {"status": "ok"}

    return application


app: FastAPI = create_app()
