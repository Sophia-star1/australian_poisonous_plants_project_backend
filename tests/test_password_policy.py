"""Password validation tests.

The signup schema previously allowed up to 128 characters while bcrypt only
accepts 72 bytes, so a long password crashed the request with a 500.
"""

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.core.security import BCRYPT_MAX_PASSWORD_BYTES, get_password_hash, verify_password
from app.schemas.auth import MIN_PASSWORD_LENGTH, SignUpRequest


def test_password_at_byte_limit_is_accepted() -> None:
    """Exactly 72 bytes still hashes."""
    pw = "a" * BCRYPT_MAX_PASSWORD_BYTES
    assert SignUpRequest(email="a@example.com", password=pw).password == pw
    assert verify_password(pw, get_password_hash(pw))


def test_password_over_byte_limit_is_rejected() -> None:
    """Regression: 100 characters used to reach bcrypt and raise a 500."""
    with pytest.raises(ValidationError):
        SignUpRequest(email="a@example.com", password="a" * 100)


def test_multibyte_password_measured_in_bytes_not_characters() -> None:
    """25 CJK characters is 75 UTF-8 bytes — over the limit despite the count."""
    pw = "密" * 25
    assert len(pw) == 25
    assert len(pw.encode()) == 75
    with pytest.raises(ValidationError):
        SignUpRequest(email="a@example.com", password=pw)


def test_short_password_is_rejected() -> None:
    """Six characters no longer passes; the minimum is now eight."""
    with pytest.raises(ValidationError):
        SignUpRequest(email="a@example.com", password="123456")
    ok = "a" * MIN_PASSWORD_LENGTH
    assert SignUpRequest(email="a@example.com", password=ok).password == ok


def test_hash_helper_guards_the_limit_directly() -> None:
    """Defence in depth: the hashing helper refuses oversized input itself."""
    with pytest.raises(ValueError, match="72 bytes"):
        get_password_hash("a" * 73)


@pytest.mark.asyncio
async def test_signup_long_password_returns_422_not_500(async_client: AsyncClient) -> None:
    """The endpoint rejects it as a validation error, before touching the DB."""
    response = await async_client.post(
        "/api/v1/auth/signup",
        json={"email": "long@example.com", "password": "a" * 100},
    )
    assert response.status_code == 422
    assert response.json()["code"] == 422
