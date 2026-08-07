"""JWT token management and password hashing utilities."""

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.config import settings

#: Value of the `type` claim on short-lived tokens used to call the API.
ACCESS_TOKEN_TYPE = "access"
#: Value of the `type` claim on long-lived tokens, only valid at /auth/refresh.
REFRESH_TOKEN_TYPE = "refresh"


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
    return _build_token(data, delta, token_type=ACCESS_TOKEN_TYPE)


def create_refresh_token(
    data: dict[str, Any], expires_delta: int | None = None
) -> str:
    """Generate a signed JWT refresh token."""
    delta = timedelta(days=expires_delta or settings.REFRESH_TOKEN_EXPIRE_DAYS)
    return _build_token(data, delta, token_type=REFRESH_TOKEN_TYPE)


def verify_token(token: str, expected_type: str = ACCESS_TOKEN_TYPE) -> dict[str, Any] | None:
    """Decode and validate a JWT, returning its claims or `None`.

    The `type` claim is enforced: an access token cannot be used where a
    refresh token is expected, and — more importantly — a long-lived refresh
    token cannot be presented as an access token to reach protected endpoints.
    """
    try:
        claims: dict[str, Any] = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
    except JWTError:
        return None

    if claims.get("type") != expected_type:
        return None
    return claims


def get_password_hash(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against its bcrypt hash."""
    try:
        return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())
    except Exception:
        return False
