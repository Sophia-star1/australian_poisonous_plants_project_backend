"""Authentication business logic."""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import (
    REFRESH_TOKEN_TYPE,
    create_access_token,
    create_refresh_token,
    verify_password,
    verify_token,
)
from app.core.token_denylist import get_denylist, seconds_until
from app.models.user import User
from app.schemas.auth import LoginRequest, SignUpRequest, TokenResponse
from app.schemas.user import UserCreate, UserOut
from app.services.user_service import UserService

#: The one answer signup gives for an address it cannot register, however it
#: found out.
_SIGNUP_CONFLICT = (
    "Could not create the account. If you already have one, try signing in "
    "or resetting your password."
)


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
            # Deliberately generic: "This email is already registered" let
            # anyone probe the API to learn which addresses hold an account.
            # The detail belongs in the email we would send to the address
            # itself, not in a response to whoever asked.
            raise ConflictError(_SIGNUP_CONFLICT)

        try:
            user = await self.users.create(
                UserCreate(
                    email=payload.email,
                    password=payload.password,
                    full_name=payload.full_name,
                )
            )
        except IntegrityError as exc:
            # The lookup above and the INSERT are two statements, so two
            # signups for one address — a double-tapped button is enough —
            # both pass the lookup and the unique index refuses the second.
            # That is the same outcome as the lookup finding it, not a 500.
            raise ConflictError(_SIGNUP_CONFLICT) from exc
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

        A refresh token works once. Exchanging it revokes it, so the pair it
        bought replaces it rather than joining it — before this, every refresh
        token stayed usable for its full seven days however many times it had
        been rotated, and one copied off a device kept minting access tokens
        long after its owner had moved on to newer ones. The mobile client
        already routes concurrent 401s through a single refresh and stores the
        new pair, so it never presents a spent token.
        """
        claims = verify_token(refresh_token, expected_type=REFRESH_TOKEN_TYPE)
        if claims is None:
            raise UnauthorizedError("Invalid or expired refresh token.")

        jti = claims.get("jti")
        # Every token issued since August carries a jti, and refresh tokens
        # last seven days, so none without one is still valid. Refusing them
        # outright is what makes single use enforceable at all.
        if not isinstance(jti, str) or await get_denylist().is_revoked(jti):
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

        # Spent last, once everything else has passed, so a failed lookup does
        # not burn a token the user could have retried with. The claim itself
        # is atomic: of two refreshes racing with one token, exactly one wins.
        if not await get_denylist().consume(jti, seconds_until(claims.get("exp"))):
            raise UnauthorizedError("Invalid or expired refresh token.")
        return self._issue_tokens(user)

