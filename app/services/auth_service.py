"""Authentication business logic."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import (
    REFRESH_TOKEN_TYPE,
    create_access_token,
    create_refresh_token,
    verify_password,
    verify_token,
)
from app.models.user import User
from app.schemas.auth import LoginRequest, SignUpRequest, TokenResponse
from app.schemas.user import UserCreate, UserOut
from app.services.user_service import UserService


class AuthService:
    """Handle user authentication workflows."""

    def __init__(self, db: AsyncSession) -> None:
        """Initialize with a database session."""
        self.db = db
        self.users = UserService(db)

    def _issue_tokens(self, user: User) -> TokenResponse:
        """Build a token-pair response for the given user."""
        claims = {"sub": str(user.id), "email": user.email, "role": user.role}
        return TokenResponse(
            access_token=create_access_token(claims),
            refresh_token=create_refresh_token(claims),
            user=UserOut.model_validate(user),
        )

    async def sign_up(self, payload: SignUpRequest) -> TokenResponse:
        """Register a new account and immediately issue tokens.

        Matches `CreateAccountScreen.createAccount` — the mobile app
        currently shows "Account created. You can now sign in.", so a
        client that doesn't want to auto-login can simply drop the
        returned tokens.
        """
        existing = await self.users.get_by_email(payload.email)
        if existing is not None:
            raise ConflictError("This email is already registered.")

        user = await self.users.create(
            UserCreate(
                email=payload.email,
                password=payload.password,
                full_name=payload.full_name,
            )
        )
        return self._issue_tokens(user)

    async def authenticate(self, payload: LoginRequest) -> TokenResponse:
        """Validate credentials and return a token pair."""
        user = await self.users.get_by_email(payload.email)
        if user is None or not verify_password(payload.password, user.hashed_password):
            raise UnauthorizedError("Incorrect email or password.")
        if not user.is_active:
            raise UnauthorizedError("Account is disabled.")
        return self._issue_tokens(user)

    async def refresh(self, refresh_token: str) -> TokenResponse:
        """Exchange a valid refresh token for a brand-new token pair.

        The user is re-read from the database so that a disabled account or a
        changed role takes effect on refresh rather than persisting for the
        remaining lifetime of the refresh token.
        """
        claims = verify_token(refresh_token, expected_type=REFRESH_TOKEN_TYPE)
        if claims is None:
            raise UnauthorizedError("Invalid or expired refresh token.")

        sub = claims.get("sub")
        if sub is None:
            raise UnauthorizedError("Invalid token payload.")
        try:
            user_id = int(str(sub))
        except ValueError as exc:
            raise UnauthorizedError("Invalid token payload.") from exc

        user = await self.users.get_by_id(user_id)
        if user is None:
            raise UnauthorizedError("User no longer exists.")
        if not user.is_active:
            raise UnauthorizedError("Account is disabled.")
        return self._issue_tokens(user)

