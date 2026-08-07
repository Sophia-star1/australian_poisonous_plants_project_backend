"""Authentication request and response schemas."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, EmailStr, Field

from app.core.security import BCRYPT_MAX_PASSWORD_BYTES
from app.schemas.user import UserOut

#: Minimum password length. Raised from 6 — six characters is trivially
#: brute-forceable and the login endpoint has no per-account lockout.
MIN_PASSWORD_LENGTH = 8


def _within_bcrypt_limit(value: str) -> str:
    """Reject passwords bcrypt cannot hash.

    bcrypt truncates or errors past 72 *bytes*, and a character-count limit
    is not equivalent: a password of emoji or CJK characters blows the byte
    budget well before it hits any plausible character cap.
    """
    if len(value.encode()) > BCRYPT_MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password must be at most {BCRYPT_MAX_PASSWORD_BYTES} bytes when UTF-8 encoded."
        )
    return value


Password = Annotated[
    str,
    Field(min_length=MIN_PASSWORD_LENGTH),
    AfterValidator(_within_bcrypt_limit),
]


class LoginRequest(BaseModel):
    """Credentials for user login."""

    email: EmailStr
    password: str


class SignUpRequest(BaseModel):
    """Payload for creating a new account from the mobile app.

    Matches `CreateAccountScreen` (email, password, optional full name in
    `options.data.full_name`).
    """

    email: EmailStr
    password: Password
    full_name: str | None = None


class RefreshRequest(BaseModel):
    """Exchange a refresh token for a fresh token pair."""

    refresh_token: str


class TokenResponse(BaseModel):
    """JWT token pair returned after authentication."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserOut | None = None


class SessionResponse(BaseModel):
    """Lightweight session payload returned by `GET /auth/session`."""

    user: UserOut | None = None
