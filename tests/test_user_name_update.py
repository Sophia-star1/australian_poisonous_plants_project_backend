"""Tests for the self-service display-name endpoint (`PATCH /users/me`)."""

from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app import dependencies
from app.core.security import create_access_token
from app.models.user import User
from app.schemas.role import UserRole
from app.schemas.user import UserNameUpdate


def _user(user_id: int, full_name: str) -> User:
    """Build a detached User instance; no session or database involved."""
    user = User()
    user.id = user_id
    user.email = f"user{user_id}@example.com"
    user.full_name = full_name
    user.is_active = True
    user.role = UserRole.USER.value
    return user


def _auth(user_id: int = 1) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user_id)})}"}


# ── what the schema accepts ───────────────────────────────────────────────────


def test_name_schema_strips_surrounding_whitespace() -> None:
    """Leading and trailing spaces are not part of the stored name."""
    assert UserNameUpdate(full_name="  Jane Doe  ").full_name == "Jane Doe"


def test_name_schema_rejects_blank_and_oversized_names() -> None:
    """A name must survive stripping as 1-255 characters."""
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(ValidationError):
            UserNameUpdate(full_name=blank)
    with pytest.raises(ValidationError):
        UserNameUpdate(full_name="x" * 256)


# ── the endpoint ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_can_change_their_own_name(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The change applies to the signed-in account, nobody else's."""
    captured: dict[str, Any] = {}

    async def _get_by_id(self: Any, wanted: int) -> User:
        return _user(wanted, "Old Name")

    async def _set_full_name(self: Any, user_id: int, full_name: str) -> User:
        captured["user_id"], captured["full_name"] = user_id, full_name
        return _user(user_id, full_name)

    # UserService is the same class object in every module, so one patch
    # serves both the authenticating lookup and the endpoint itself.
    monkeypatch.setattr(dependencies.UserService, "get_by_id", _get_by_id)
    monkeypatch.setattr(dependencies.UserService, "set_full_name", _set_full_name)

    response = await async_client.patch(
        "/api/v1/users/me", json={"full_name": "Jane Doe"}, headers=_auth(user_id=7)
    )
    assert response.status_code == 200
    assert response.json()["data"]["full_name"] == "Jane Doe"
    assert captured == {"user_id": 7, "full_name": "Jane Doe"}


@pytest.mark.asyncio
async def test_name_update_requires_a_token(async_client: AsyncClient) -> None:
    """Anonymous callers get 401, not a way in."""
    response = await async_client.patch("/api/v1/users/me", json={"full_name": "Jane"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_blank_name_returns_422(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name of only spaces fails validation rather than being stored."""

    async def _get_by_id(self: Any, wanted: int) -> User:
        return _user(wanted, "Old Name")

    monkeypatch.setattr(dependencies.UserService, "get_by_id", _get_by_id)

    response = await async_client.patch(
        "/api/v1/users/me", json={"full_name": "   "}, headers=_auth()
    )
    assert response.status_code == 422
    assert response.json()["code"] == 422


@pytest.mark.asyncio
async def test_oversized_name_returns_422(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The column is varchar(255); a longer name is refused at the edge."""

    async def _get_by_id(self: Any, wanted: int) -> User:
        return _user(wanted, "Old Name")

    monkeypatch.setattr(dependencies.UserService, "get_by_id", _get_by_id)

    response = await async_client.patch(
        "/api/v1/users/me", json={"full_name": "x" * 256}, headers=_auth()
    )
    assert response.status_code == 422
    assert response.json()["code"] == 422


@pytest.mark.asyncio
async def test_unknown_fields_are_rejected(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the name is accepted here — no email, password or role smuggling."""

    async def _get_by_id(self: Any, wanted: int) -> User:
        return _user(wanted, "Old Name")

    monkeypatch.setattr(dependencies.UserService, "get_by_id", _get_by_id)

    response = await async_client.patch(
        "/api/v1/users/me",
        json={"full_name": "Jane", "email": "someone@example.com"},
        headers=_auth(),
    )
    assert response.status_code == 422
