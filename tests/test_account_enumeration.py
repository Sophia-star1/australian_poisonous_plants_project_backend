"""Tests that the API does not reveal which addresses hold an account.

Login already returned a single message for both 'no such user' and 'wrong
password'. Signup gave the game away with 'This email is already registered.'
"""

from typing import Any

import pytest
from httpx import AsyncClient

from app import dependencies
from app.core.security import create_access_token
from app.models.user import User
from app.schemas.auth import SignUpRequest
from app.services.auth_service import AuthService
from app.services.user_service import UserService


def _existing_user() -> User:
    user = User()
    user.id = 1
    user.email = "taken@example.com"
    user.full_name = "Taken"
    user.is_active = True
    user.role = "user"
    return user


@pytest.mark.asyncio
async def test_signup_conflict_does_not_confirm_the_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the message named the exact condition it detected."""

    async def _found(self: Any, email: str) -> User:
        return _existing_user()

    monkeypatch.setattr(UserService, "get_by_email", _found)

    service = AuthService(db=None)  # type: ignore[arg-type]
    with pytest.raises(Exception) as excinfo:
        await service.sign_up(
            SignUpRequest(email="taken@example.com", password="a-valid-password")
        )

    message = str(getattr(excinfo.value, "detail", excinfo.value))
    assert "already registered" not in message.lower()
    assert "could not create the account" in message.lower()


@pytest.mark.asyncio
async def test_login_message_is_identical_for_both_failures(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown address and wrong password must be indistinguishable."""
    calls: list[str] = []

    async def _lookup(self: Any, email: str) -> User | None:
        calls.append(email)
        return _existing_user() if email == "taken@example.com" else None

    monkeypatch.setattr(UserService, "get_by_email", _lookup)

    missing = await async_client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "some-password"},
    )
    wrong = await async_client.post(
        "/api/v1/auth/login",
        json={"email": "taken@example.com", "password": "some-password"},
    )

    assert missing.status_code == wrong.status_code == 401
    assert missing.json()["message"] == wrong.json()["message"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_disabled_account_is_anonymous_to_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_optional_user must agree with get_current_active_user.

    It used to return deactivated accounts, so a disabled user could still
    have submissions attributed to them.
    """

    async def _disabled(self: Any, user_id: int) -> User:
        user = _existing_user()
        user.is_active = False
        return user

    monkeypatch.setattr(dependencies.UserService, "get_by_id", _disabled)

    result = await dependencies.get_optional_user(
        token=create_access_token({"sub": "1"}), db=None  # type: ignore[arg-type]
    )
    assert result is None
