"""Rate limiting tests for the credential endpoints.

The endpoint-level cases stub out `AuthService` so nothing reaches the
database: the limiter runs before the service is called, which is exactly the
property under test, and the suite stays fast and hermetic.
"""

from typing import Any

import pytest
from httpx import AsyncClient

from app.config import settings
from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.rate_limit import InMemoryRateLimiter, reset_rate_limiter
from app.services.auth_service import AuthService


@pytest.fixture(autouse=True)
def _fresh_limiter() -> None:
    """Each test starts with an empty counter."""
    reset_rate_limiter()


@pytest.fixture
def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the auth service fail fast without touching Postgres."""

    async def _reject(self: AuthService, *args: Any, **kwargs: Any) -> None:
        raise UnauthorizedError("Incorrect email or password.")

    async def _conflict(self: AuthService, *args: Any, **kwargs: Any) -> None:
        raise ConflictError("This email is already registered.")

    monkeypatch.setattr(AuthService, "authenticate", _reject)
    monkeypatch.setattr(AuthService, "sign_up", _conflict)


async def test_limiter_allows_up_to_the_limit() -> None:
    """The nth attempt is allowed; the n+1th is not."""
    limiter = InMemoryRateLimiter()
    for i in range(3):
        result = await limiter.hit("k", limit=3, window_seconds=60)
        assert result.allowed, f"attempt {i + 1} should be allowed"
    assert not (await limiter.hit("k", limit=3, window_seconds=60)).allowed


async def test_limiter_reports_remaining_and_retry_after() -> None:
    """Callers get enough information to build a useful error message."""
    limiter = InMemoryRateLimiter()
    result = await limiter.hit("k", limit=5, window_seconds=60)
    assert result.remaining == 4
    assert 0 < result.retry_after <= 60


async def test_limiter_keys_are_independent() -> None:
    """Exhausting one identifier must not throttle another."""
    limiter = InMemoryRateLimiter()
    for _ in range(3):
        await limiter.hit("a", limit=3, window_seconds=60)
    assert not (await limiter.hit("a", limit=3, window_seconds=60)).allowed
    assert (await limiter.hit("b", limit=3, window_seconds=60)).allowed


async def test_reset_clears_the_counter() -> None:
    """A successful login should not leave the account throttled."""
    limiter = InMemoryRateLimiter()
    for _ in range(3):
        await limiter.hit("k", limit=3, window_seconds=60)
    await limiter.reset("k")
    assert (await limiter.hit("k", limit=3, window_seconds=60)).allowed


async def test_expired_windows_are_purged() -> None:
    """The in-memory map must not grow without bound."""
    limiter = InMemoryRateLimiter()
    await limiter.hit("old", limit=1, window_seconds=0)
    await limiter.hit("new", limit=1, window_seconds=60)
    assert "old" not in limiter._hits


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_db")
async def test_login_throttles_after_repeated_attempts(async_client: AsyncClient) -> None:
    """Regression: production accepted 10/10 wrong passwords with no pushback."""
    body = {"email": "bruteforce@example.com", "password": "wrong-password"}
    statuses = [
        (await async_client.post("/api/v1/auth/login", json=body)).status_code
        for _ in range(settings.LOGIN_RATE_LIMIT + 3)
    ]

    assert 401 in statuses, "the first attempts should still reach the service"
    assert 429 in statuses, f"expected throttling, got {statuses}"
    assert statuses.index(429) <= settings.LOGIN_RATE_LIMIT

    throttled = await async_client.post("/api/v1/auth/login", json=body)
    assert throttled.status_code == 429
    assert throttled.json()["code"] == 429
    assert int(throttled.headers["Retry-After"]) > 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_db")
async def test_login_throttles_per_account_across_addresses(async_client: AsyncClient) -> None:
    """Rotating the source address does not reset an account's budget.

    X-Forwarded-For is attacker-controlled, so the IP-scoped limit alone would
    be trivially bypassed. The account-scoped limit is what makes the pair work.
    """
    body = {"email": "victim@example.com", "password": "wrong-password"}
    last = None
    for i in range(settings.LOGIN_RATE_LIMIT + 2):
        last = await async_client.post(
            "/api/v1/auth/login", json=body, headers={"X-Forwarded-For": f"10.0.0.{i}"}
        )
    assert last is not None
    assert last.status_code == 429


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_db")
async def test_signup_is_throttled_per_ip(async_client: AsyncClient) -> None:
    """Bulk account creation from one address is capped."""
    statuses = []
    for i in range(settings.SIGNUP_RATE_LIMIT + 2):
        response = await async_client.post(
            "/api/v1/auth/signup",
            json={"email": f"bulk{i}@example.com", "password": "valid-password"},
        )
        statuses.append(response.status_code)
    assert 429 in statuses, f"expected throttling, got {statuses}"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_db")
async def test_other_endpoints_are_not_throttled(async_client: AsyncClient) -> None:
    """The limiter is scoped to credential endpoints only."""
    for _ in range(settings.LOGIN_RATE_LIMIT + 5):
        response = await async_client.get("/health")
        assert response.status_code == 200
