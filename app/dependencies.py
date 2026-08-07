"""Global FastAPI dependency injection providers."""

from collections.abc import Awaitable, Callable

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import verify_token
from app.core.token_denylist import get_denylist
from app.db.session import get_db
from app.models.user import User
from app.schemas.role import UserRole
from app.services.user_service import UserService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


async def _reject_if_revoked(claims: dict[str, object]) -> bool:
    """Return True when the token behind `claims` has been logged out."""
    jti = claims.get("jti")
    if not isinstance(jti, str):
        # Tokens issued before jti existed cannot be revoked individually.
        # They still expire on their own; treat them as valid until then.
        return False
    return await get_denylist().is_revoked(jti)


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
    if await _reject_if_revoked(claims):
        raise UnauthorizedError("Token has been revoked.")

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
    invoke either signed in or anonymously. A disabled account is treated as
    anonymous rather than authenticated, matching `get_current_active_user`;
    the alternative would let a deactivated user keep attributing records to
    themselves.
    """
    if not token:
        return None
    claims = verify_token(token)
    if not claims:
        return None
    if await _reject_if_revoked(claims):
        return None
    sub = claims.get("sub")
    if sub is None:
        return None
    try:
        user_id = int(str(sub))
    except ValueError:
        return None

    user = await UserService(db).get_by_id(user_id)
    if user is None or not user.is_active:
        return None
    return user


def require_role(role: UserRole) -> Callable[..., Awaitable[User]]:
    """Return a dependency that enforces a specific user role.

    Takes a `UserRole` rather than a bare string: the comparison is exact, so
    a misspelled literal would reject everyone forever without raising.
    """

    async def _role_checker(
        current_user: User = Depends(get_current_active_user),
    ) -> User:
        if current_user.role != role.value:
            raise ForbiddenError(f"Role '{role.value}' required.")
        return current_user

    return _role_checker
