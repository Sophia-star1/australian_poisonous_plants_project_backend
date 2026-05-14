"""Authentication request and response schemas."""

from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    """Credentials for user login."""

    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    """JWT token pair returned after authentication."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    """Payload for refreshing or revoking a token."""

    refresh_token: str
