"""JWT token management and password hashing utilities."""

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.config import settings


def _now() -> datetime:
    """Return an aware UTC `datetime` (separate helper for easier mocking)."""
    return datetime.now(timezone.utc)


def _build_token(
    data: dict[str, Any], expires_delta: timedelta, token_type: str
) -> str:
    """Encode the given claims into a signed JWT."""
    to_encode: dict[str, Any] = {**data}
    expire = _now() + expires_delta
    to_encode.update({"exp": expire, "iat": _now(), "type": token_type})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_access_token(
    data: dict[str, Any], expires_delta: int | None = None
) -> str:
    """Generate a signed JWT access token."""
    delta = timedelta(minutes=expires_delta or settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return _build_token(data, delta, token_type="access")


def create_refresh_token(
    data: dict[str, Any], expires_delta: int | None = None
) -> str:
    """Generate a signed JWT refresh token."""
    delta = timedelta(days=expires_delta or settings.REFRESH_TOKEN_EXPIRE_DAYS)
    return _build_token(data, delta, token_type="refresh")


def verify_token(token: str) -> dict[str, Any] | None:
    """Decode and validate a JWT token, returning claims or `None`."""
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None


def get_password_hash(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())
    except Exception:
        return False
