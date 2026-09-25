"""Tests for role handling and the user administration endpoints."""

from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app import dependencies
from app.api.v1 import users as users_api
from app.core.security import create_access_token
from app.models.user import User
from app.schemas.role import DEFAULT_ROLE, UserRole
from app.schemas.user import UserRoleUpdate


def _user(user_id: int, role: str, email: str | None = None) -> User:
    """Build a detached User instance; no session or database involved."""
    user = User()
    user.id = user_id
    user.email = email or f"user{user_id}@example.com"
    user.full_name = f"User {user_id}"
    user.is_active = True
    user.role = role
    return user


@pytest.fixture
def as_role(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Authenticate every request as a user with the given id and role."""

    def _apply(role: str, user_id: int = 1) -> None:
        async def _get_by_id(self: Any, wanted: int) -> User:
            return _user(wanted, role if wanted == user_id else UserRole.USER.value)

        monkeypatch.setattr(dependencies.UserService, "get_by_id", _get_by_id)

    return _apply


def _auth(user_id: int = 1) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user_id)})}"}


def test_role_enum_values_match_stored_strings() -> None:
    """StrEnum members must equal the plain strings already in the database."""
    assert UserRole.ADMIN == "admin"
    assert UserRole.USER == "user"
    assert DEFAULT_ROLE is UserRole.USER


def test_unknown_role_is_rejected_by_the_schema() -> None:
    """A typo becomes a validation error instead of a permanent silent 403."""
    with pytest.raises(ValidationError):
        UserRoleUpdate(role="administrator")
    assert UserRoleUpdate(role="admin").role is UserRole.ADMIN


@pytest.mark.asyncio
async def test_listing_users_requires_admin(async_client: AsyncClient, as_role: Any) -> None:
    """A normal account cannot enumerate users."""
    as_role(UserRole.USER.value)
    response = await async_client.get("/api/v1/users/", headers=_auth())
    assert response.status_code == 403
    assert response.json()["message"] == "Role 'admin' required."


@pytest.mark.asyncio
async def test_listing_users_requires_a_token(async_client: AsyncClient) -> None:
    """Anonymous callers get 401, not 403."""
    assert (await async_client.get("/api/v1/users/")).status_code == 401


@pytest.mark.asyncio
async def test_admin_can_change_a_role(
    async_client: AsyncClient, as_role: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint that replaces hand-written SQL."""
    as_role(UserRole.ADMIN.value)
    captured: dict[str, Any] = {}

    async def _get_by_id(self: Any, wanted: int) -> User:
        # UserService is the same class object in both modules, so this patch
        # also serves the authenticating lookup — id 1 must stay an admin.
        return _user(wanted, UserRole.ADMIN.value if wanted == 1 else UserRole.USER.value)

    async def _set_role(self: Any, user_id: int, role: UserRole) -> User:
        captured["user_id"], captured["role"] = user_id, role
        return _user(user_id, role.value)

    monkeypatch.setattr(users_api.UserService, "get_by_id", _get_by_id)
    monkeypatch.setattr(users_api.UserService, "set_role", _set_role)

    response = await async_client.patch(
        "/api/v1/users/7/role", json={"role": "admin"}, headers=_auth()
    )
    assert response.status_code == 200
    assert response.json()["data"]["role"] == "admin"
    assert captured == {"user_id": 7, "role": UserRole.ADMIN}


@pytest.mark.asyncio
async def test_admin_cannot_demote_themselves(
    async_client: AsyncClient, as_role: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common self-lockout accident."""
    as_role(UserRole.ADMIN.value)

    async def _get_by_id(self: Any, wanted: int) -> User:
        return _user(wanted, UserRole.ADMIN.value)

    monkeypatch.setattr(users_api.UserService, "get_by_id", _get_by_id)

    response = await async_client.patch(
        "/api/v1/users/1/role", json={"role": "user"}, headers=_auth(user_id=1)
    )
    assert response.status_code == 403
    assert "your own admin role" in response.json()["message"]


@pytest.mark.asyncio
async def test_last_admin_cannot_be_demoted(
    async_client: AsyncClient, as_role: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing the final administrator would need database access to undo."""
    as_role(UserRole.ADMIN.value)

    async def _get_by_id(self: Any, wanted: int) -> User:
        return _user(wanted, UserRole.ADMIN.value)

    async def _count(self: Any, role: UserRole) -> int:
        return 1

    monkeypatch.setattr(users_api.UserService, "get_by_id", _get_by_id)
    monkeypatch.setattr(users_api.UserService, "count_with_role", _count)

    response = await async_client.patch(
        "/api/v1/users/2/role", json={"role": "user"}, headers=_auth(user_id=1)
    )
    assert response.status_code == 403
    assert "last remaining administrator" in response.json()["message"]


@pytest.mark.asyncio
async def test_demoting_an_admin_is_allowed_when_others_remain(
    async_client: AsyncClient, as_role: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is about the last one, not about admins in general."""
    as_role(UserRole.ADMIN.value)

    async def _get_by_id(self: Any, wanted: int) -> User:
        return _user(wanted, UserRole.ADMIN.value)

    async def _count(self: Any, role: UserRole) -> int:
        return 3

    async def _set_role(self: Any, user_id: int, role: UserRole) -> User:
        return _user(user_id, role.value)

    monkeypatch.setattr(users_api.UserService, "get_by_id", _get_by_id)
    monkeypatch.setattr(users_api.UserService, "count_with_role", _count)
    monkeypatch.setattr(users_api.UserService, "set_role", _set_role)

    response = await async_client.patch(
        "/api/v1/users/2/role", json={"role": "user"}, headers=_auth(user_id=1)
    )
    assert response.status_code == 200
    assert response.json()["data"]["role"] == "user"


@pytest.mark.asyncio
async def test_unknown_role_returns_422(async_client: AsyncClient, as_role: Any) -> None:
    """Invalid input is a validation error, in the standard envelope."""
    as_role(UserRole.ADMIN.value)
    response = await async_client.patch(
        "/api/v1/users/2/role", json={"role": "superuser"}, headers=_auth()
    )
    assert response.status_code == 422
    assert response.json()["code"] == 422
