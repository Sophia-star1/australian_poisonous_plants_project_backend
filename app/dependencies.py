"""Global FastAPI dependency injection providers."""

from collections.abc import Callable

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.user import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract and validate the current user from the JWT token."""
    ...


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Ensure the current user account is active."""
    ...


def require_role(role: str) -> Callable[..., User]:
    """Return a dependency that enforces a specific user role."""

    async def _role_checker(
        current_user: User = Depends(get_current_active_user),
    ) -> User:
        """Check that the current user has the required role."""
        ...

    return _role_checker
