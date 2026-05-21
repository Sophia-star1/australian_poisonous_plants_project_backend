"""Global FastAPI dependency injection providers."""

from collections.abc import Callable

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import verify_token
from app.db.session import get_db
from app.models.user import User
from app.services.user_service import UserService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract and validate the current user from the JWT token."""
    if not token:
        raise UnauthorizedError("Authentication required.")

    claims = verify_token(token)
    if claims is None:
        raise UnauthorizedError("Invalid or expired token.")

    sub = claims.get("sub")
    if sub is None:
        raise UnauthorizedError("Invalid token payload.")
    try:
        user_id = int(str(sub))
    except ValueError as exc:
        raise UnauthorizedError("Invalid token payload.") from exc

    user = await UserService(db).get_by_id(user_id)
    if user is None:
        raise UnauthorizedError("User no longer exists.")
    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Ensure the current user account is active."""
    if not current_user.is_active:
        raise ForbiddenError("Account is disabled.")
    return current_user


async def get_optional_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """Return the current user if a valid token was supplied, otherwise `None`.

    Used by endpoints (e.g. `POST /submissions`) that the mobile app may
    invoke either signed in or anonymously.
    """
    if not token:
        return None
    claims = verify_token(token)
    if not claims:
        return None
    sub = claims.get("sub")
    if sub is None:
        return None
    try:
        user_id = int(str(sub))
    except ValueError:
        return None
    return await UserService(db).get_by_id(user_id)


def require_role(role: str) -> Callable[..., User]:
    """Return a dependency that enforces a specific user role."""

    async def _role_checker(
        current_user: User = Depends(get_current_active_user),
    ) -> User:
        if current_user.role != role:
            raise ForbiddenError(f"Role '{role}' required.")
        return current_user

    return _role_checker
