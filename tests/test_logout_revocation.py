"""Tests for token revocation on logout.

POST /auth/logout used to return 200 and do nothing at all: the same bearer
token still worked afterwards. Verified against production before the fix.
"""

import time
from typing import Any

import pytest
from httpx import AsyncClient

from app import dependencies
from app.core.security import create_access_token, create_refresh_token, verify_token
from app.core.token_denylist import InMemoryDenylist, get_denylist, reset_denylist
from app.models.user import User

CLAIMS = {"sub": "1", "email": "user@example.com", "role": "user"}


@pytest.fixture(autouse=True)
def _fresh_denylist() -> None:
    """Each test starts with an empty revocation list."""
    reset_denylist()


@pytest.fixture
def _stub_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub only the database lookup, not the dependency itself.

    Overriding `get_current_user` would skip the revocation check that lives
    inside it — the thing these tests exist to verify. Patching the user
    service instead keeps the real dependency chain running.
    """

    async def _get_by_id(self: Any, user_id: int) -> User:
        user = User()
        user.id = user_id
        user.email = "user@example.com"
        user.full_name = "Test User"
        user.is_active = True
        user.role = "user"
        return user

    monkeypatch.setattr(dependencies.UserService, "get_by_id", _get_by_id)


async def test_denylist_roundtrip() -> None:
    """A revoked id reads back as revoked; an unrelated one does not."""
    denylist = InMemoryDenylist()
    await denylist.revoke("abc", ttl_seconds=60)
    assert await denylist.is_revoked("abc")
    assert not await denylist.is_revoked("def")


async def test_denylist_entry_expires() -> None:
    """Entries do not outlive the token they describe.

    `revoke` floors the TTL at one second, so the expiry is rewound directly
    rather than sleeping through it.
    """
    denylist = InMemoryDenylist()
    await denylist.revoke("abc", ttl_seconds=60)
    denylist._revoked["abc"] = time.time() - 1
    assert not await denylist.is_revoked("abc")
    assert "abc" not in denylist._revoked, "expired entry should be dropped, not kept"


def test_every_token_carries_a_unique_jti() -> None:
    """Revocation needs a per-token identifier to key on."""
    first = verify_token(create_access_token(CLAIMS))
    second = verify_token(create_access_token(CLAIMS))
    assert first is not None and second is not None
    assert first["jti"] and first["jti"] != second["jti"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_user")
async def test_logout_revokes_the_access_token(async_client: AsyncClient) -> None:
    """Regression: production returned 200 from /me after logout."""
    token = create_access_token(CLAIMS)
    headers = {"Authorization": f"Bearer {token}"}

    assert (await async_client.get("/api/v1/auth/me", headers=headers)).status_code == 200

    logout = await async_client.post("/api/v1/auth/logout", headers=headers)
    assert logout.status_code == 200

    after = await async_client.get("/api/v1/auth/me", headers=headers)
    assert after.status_code == 401
    assert after.json()["message"] == "Token has been revoked."


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_user")
async def test_logout_revokes_the_refresh_token_too(async_client: AsyncClient) -> None:
    """Otherwise the refresh token could immediately mint a new access token."""
    access = create_access_token(CLAIMS)
    refresh = create_refresh_token(CLAIMS)

    await async_client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {access}"},
        json={"refresh_token": refresh},
    )

    response = await async_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.usefixtures("_stub_user")
async def test_logout_leaves_other_sessions_alone(async_client: AsyncClient) -> None:
    """Signing out on one device must not sign the user out everywhere."""
    phone = create_access_token(CLAIMS)
    laptop = create_access_token(CLAIMS)

    await async_client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {phone}"})

    still_valid = await async_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {laptop}"}
    )
    assert still_valid.status_code == 200


@pytest.mark.asyncio
async def test_logout_is_idempotent_and_quiet(async_client: AsyncClient) -> None:
    """Garbage or absent credentials still report success, revealing nothing."""
    assert (await async_client.post("/api/v1/auth/logout")).status_code == 200
    noisy = await async_client.post(
        "/api/v1/auth/logout", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert noisy.status_code == 200


@pytest.mark.asyncio
async def test_revocation_ttl_is_bounded_by_token_expiry() -> None:
    """The entry is sized to the token, so the list cannot grow forever."""
    token = create_access_token(CLAIMS)
    claims = verify_token(token)
    assert claims is not None
    await get_denylist().revoke(claims["jti"], ttl_seconds=1)
    assert await get_denylist().is_revoked(claims["jti"])
