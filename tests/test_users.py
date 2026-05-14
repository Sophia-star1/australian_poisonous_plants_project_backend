"""User CRUD endpoint test scaffolding."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_list_users(async_client: AsyncClient) -> None:
    """Test paginated user listing."""
    assert True


@pytest.mark.asyncio
async def test_create_user(async_client: AsyncClient) -> None:
    """Test user registration."""
    assert True


@pytest.mark.asyncio
async def test_get_user(async_client: AsyncClient) -> None:
    """Test fetching a single user."""
    assert True


@pytest.mark.asyncio
async def test_update_user(async_client: AsyncClient) -> None:
    """Test partial user update."""
    assert True


@pytest.mark.asyncio
async def test_delete_user(async_client: AsyncClient) -> None:
    """Test user soft-deletion."""
    assert True
