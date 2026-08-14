"""Authentication endpoint tests that do not require a live database.

Every case here is rejected before any query runs, so the suite stays
hermetic. Database-backed flows (signup, login, role changes) are covered by
the integration script in `scripts/` instead.
"""

import pytest
from httpx import AsyncClient

from app.core.security import create_access_token, create_refresh_token

CLAIMS = {"sub": "1", "email": "user@example.com", "role": "user"}


@pytest.mark.asyncio
async def test_refresh_rejects_access_token(async_client: AsyncClient) -> None:
    """POST /auth/refresh must not accept an access token."""
    response = await async_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": create_access_token(CLAIMS)}
    )
    assert response.status_code == 401
    assert response.json()["message"] == "Invalid or expired refresh token."


@pytest.mark.asyncio
async def test_refresh_rejects_garbage(async_client: AsyncClient) -> None:
    """A malformed refresh token yields 401, not a 500."""
    response = await async_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": "not-a-jwt"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_rejects_refresh_token(async_client: AsyncClient) -> None:
    """Regression: a refresh token must not authenticate a protected request."""
    response = await async_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {create_refresh_token(CLAIMS)}"},
    )
    assert response.status_code == 401
    assert response.json()["message"] == "Invalid or expired token."


@pytest.mark.asyncio
async def test_protected_endpoint_requires_token(async_client: AsyncClient) -> None:
    """No credentials at all is a 401 with a clear message."""
    response = await async_client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["message"] == "Authentication required."
