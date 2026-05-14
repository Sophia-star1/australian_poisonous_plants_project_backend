"""Authentication business logic."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.auth import LoginRequest, TokenResponse


class AuthService:
    """Handle user authentication workflows."""

    def __init__(self, db: AsyncSession) -> None:
        """Initialize with a database session."""
        self.db = db

    async def authenticate(self, payload: LoginRequest) -> TokenResponse:
        """Validate credentials and return token pair."""
        ...

    async def refresh(self, refresh_token: str) -> TokenResponse:
        """Issue new tokens from a valid refresh token."""
        ...

    async def revoke(self, refresh_token: str) -> None:
        """Invalidate a refresh token."""
        ...
