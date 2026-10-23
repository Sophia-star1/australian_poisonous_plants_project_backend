"""Generic API response and pagination schemas."""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """Standardized API response envelope."""

    code: int = 200
    message: str = "success"
    data: T | None = None

    @staticmethod
    def success(data: T | None = None, message: str = "success") -> "ApiResponse[T]":
        """Create a successful response."""
        return ApiResponse(code=200, message=message, data=data)

    @staticmethod
    def error(code: int = 400, message: str = "error") -> "ApiResponse[None]":
        """Create an error response."""
        return ApiResponse(code=code, message=message, data=None)


class PageResult(BaseModel, Generic[T]):
    """Paginated result container."""

    items: list[T]
    total: int
    page: int
    page_size: int
