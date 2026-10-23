"""Shared pytest fixtures for async API testing."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.rate_limit import reset_rate_limiter
from app.main import app


@pytest.fixture(autouse=True)
def _fresh_rate_limits() -> None:
    """Rate-limit counters start empty in every test.

    The limiter is a process-wide singleton, so without this the uploads made
    by one test would use up the budget of the next.
    """
    reset_rate_limiter()


@pytest.fixture
async def async_client() -> AsyncClient:  # type: ignore[misc]
    """Provide an async HTTP test client."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client  # type: ignore[misc]
