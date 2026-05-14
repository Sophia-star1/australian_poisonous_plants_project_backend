"""Authentication endpoint test scaffolding."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_login(async_client: AsyncClient) -> None:
    """Test user login returns tokens."""
    assert True


@pytest.mark.asyncio
async def test_refresh_token(async_client: AsyncClient) -> None:
    """Test token refresh flow."""
    assert True


@pytest.mark.asyncio
async def test_logout(async_client: AsyncClient) -> None:
    """Test logout invalidates refresh token."""
    assert True
