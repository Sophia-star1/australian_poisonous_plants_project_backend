"""A refresh token buys one new pair, then stops working.

Before, `/auth/refresh` checked the denylist but never added to it, so a
refresh token stayed usable for its full seven days however many times it had
been exchanged. One copied off a device kept minting access tokens after its
owner had long since rotated past it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from jose import jwt

from app.config import settings
from app.core.security import create_refresh_token
from app.core.token_denylist import InMemoryDenylist, RedisDenylist, reset_denylist
from app.models.user import User
from app.services.user_service import UserService

CLAIMS = {"sub": "1", "email": "user@example.com", "role": "user"}


@pytest.fixture(autouse=True)
def _fresh_denylist() -> None:
    reset_denylist()


@pytest.fixture(autouse=True)
def _stub_user(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _get_by_id(self: Any, user_id: int) -> User:
        user = User()
        user.id = user_id
        user.email = "user@example.com"
        user.full_name = "Test User"
        user.is_active = True
        user.role = "user"
        return user

    monkeypatch.setattr(UserService, "get_by_id", _get_by_id)


async def _refresh(client: AsyncClient, token: str) -> Any:
    return await client.post("/api/v1/auth/refresh", json={"refresh_token": token})


@pytest.mark.asyncio
async def test_a_refresh_token_works_once(async_client: AsyncClient) -> None:
    token = create_refresh_token(CLAIMS)

    first = await _refresh(async_client, token)
    again = await _refresh(async_client, token)

    assert first.status_code == 200
    assert again.status_code == 401
    assert again.json()["message"] == "Invalid or expired refresh token."


@pytest.mark.asyncio
async def test_the_new_refresh_token_carries_on(async_client: AsyncClient) -> None:
    """Rotation, not a dead end: each pair's refresh token buys the next."""
    token = create_refresh_token(CLAIMS)
    for _ in range(3):
        response = await _refresh(async_client, token)
        assert response.status_code == 200
        token = response.json()["data"]["refresh_token"]


@pytest.mark.asyncio
async def test_two_refreshes_racing_with_one_token_cannot_both_win(
    async_client: AsyncClient,
) -> None:
    token = create_refresh_token(CLAIMS)

    results = await asyncio.gather(*(_refresh(async_client, token) for _ in range(5)))

    assert sorted(r.status_code for r in results) == [200, 401, 401, 401, 401]


@pytest.mark.asyncio
async def test_a_token_is_not_spent_by_a_request_that_fails(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient failure after validation must not log the user out."""

    async def _gone(self: Any, user_id: int) -> None:
        return None

    token = create_refresh_token(CLAIMS)
    with monkeypatch.context() as patch:
        patch.setattr(UserService, "get_by_id", _gone)
        assert (await _refresh(async_client, token)).status_code == 401

    assert (await _refresh(async_client, token)).status_code == 200


@pytest.mark.asyncio
async def test_a_logged_out_refresh_token_stays_refused(async_client: AsyncClient) -> None:
    token = create_refresh_token(CLAIMS)
    await async_client.post("/api/v1/auth/logout", json={"refresh_token": token})
    assert (await _refresh(async_client, token)).status_code == 401


@pytest.mark.asyncio
async def test_a_refresh_token_without_a_jti_is_refused(async_client: AsyncClient) -> None:
    """Single use needs something to key on. None issued since 2026-08-07 lacks it."""
    token = jwt.encode(
        {**CLAIMS, "type": "refresh", "exp": 9_999_999_999},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    assert (await _refresh(async_client, token)).status_code == 401


# ── the claim itself ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_in_memory_consume_succeeds_exactly_once() -> None:
    denylist = InMemoryDenylist()
    assert await denylist.consume("abc", 60) is True
    assert await denylist.consume("abc", 60) is False
    assert await denylist.is_revoked("abc") is True


@pytest.mark.asyncio
async def test_redis_consume_is_a_single_set_nx() -> None:
    """Atomic across instances only if it is one command, not exists-then-set."""
    calls: list[tuple[Any, ...]] = []

    class _FakeRedis:
        def __init__(self) -> None:
            self.keys: set[str] = set()

        async def set(self, key: str, value: str, ex: int, nx: bool) -> bool | None:
            calls.append((key, ex, nx))
            if nx and key in self.keys:
                return None
            self.keys.add(key)
            return True

    denylist = RedisDenylist.__new__(RedisDenylist)
    denylist._redis = _FakeRedis()  # type: ignore[assignment]

    assert await denylist.consume("abc", 60) is True
    assert await denylist.consume("abc", 60) is False
    assert calls == [("revoked:abc", 60, True), ("revoked:abc", 60, True)]
