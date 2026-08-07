"""Custom exception classes and global exception handlers."""

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class AppException(HTTPException):
    """Base application exception with a default error code."""

    def __init__(self, status_code: int = 500, detail: str = "Internal server error") -> None:
        super().__init__(status_code=status_code, detail=detail)


class NotFoundError(AppException):
    """Resource not found (404)."""

    def __init__(self, detail: str = "Resource not found") -> None:
        super().__init__(status_code=404, detail=detail)


class UnauthorizedError(AppException):
    """Authentication required or failed (401)."""

    def __init__(self, detail: str = "Not authenticated") -> None:
        super().__init__(status_code=401, detail=detail)


class ForbiddenError(AppException):
    """Insufficient permissions (403)."""

    def __init__(self, detail: str = "Permission denied") -> None:
        super().__init__(status_code=403, detail=detail)


class ConflictError(AppException):
    """Resource conflict (409)."""

    def __init__(self, detail: str = "Resource conflict") -> None:
        super().__init__(status_code=409, detail=detail)


class UnprocessableError(AppException):
    """Unprocessable entity (422)."""

    def __init__(self, detail: str = "Unprocessable entity") -> None:
        super().__init__(status_code=422, detail=detail)


class TooManyRequestsError(AppException):
    """Rate limit exceeded (429)."""

    def __init__(
        self, detail: str = "Too many requests. Try again later.", retry_after: int = 60
    ) -> None:
        super().__init__(status_code=429, detail=detail)
        self.retry_after = retry_after


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    """Handle AppException subclasses and return structured JSON error."""
    headers: dict[str, str] = {}
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": exc.detail, "data": None},
        headers=headers or None,
    )


def _format_validation_errors(exc: Exception) -> str:
    """Render pydantic's error list as one readable sentence.

    FastAPI's default 422 body is a nested `detail` array that does not match
    the `{code, message, data}` envelope every other response uses.
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    parts = []
    for err in errors():
        location = ".".join(str(item) for item in err.get("loc", ()) if item != "body")
        parts.append(f"{location or 'body'}: {err.get('msg', 'invalid')}")
    return "; ".join(parts) or str(exc)


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle request validation errors and return structured JSON error."""
    return JSONResponse(
        status_code=422,
        content={"code": 422, "message": _format_validation_errors(exc), "data": None},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all so an unexpected error still returns the API envelope.

    Without this, anything that is not an `AppException` escapes to Starlette
    and produces a bare `Internal Server Error` body — inconsistent with every
    other response and useless to a client. The traceback is logged with the
    request ID for correlation but never sent to the caller.
    """
    logger.exception(
        "unhandled_exception",
        request_id=getattr(request.state, "request_id", None),
        method=request.method,
        path=request.url.path,
        exc_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={"code": 500, "message": "Internal server error.", "data": None},
    )
