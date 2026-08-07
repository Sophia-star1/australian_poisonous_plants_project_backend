"""Authentication request and response schemas."""

from pydantic import BaseModel, EmailStr, Field

from app.schemas.user import UserOut


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
    password: str = Field(..., min_length=6, max_length=128)
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
