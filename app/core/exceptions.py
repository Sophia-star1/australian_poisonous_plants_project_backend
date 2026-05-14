"""Custom exception classes and global exception handlers."""

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


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


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    """Handle AppException subclasses and return structured JSON error."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": exc.detail, "data": None},
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle request validation errors and return structured JSON error."""
    return JSONResponse(
        status_code=422,
        content={"code": 422, "message": str(exc), "data": None},
    )
