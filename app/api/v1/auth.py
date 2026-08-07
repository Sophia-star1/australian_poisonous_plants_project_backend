"""Authentication endpoints: signup, login, logout, session.

Replaces the mobile app's direct `supabase.auth.*` calls:

* `signUp`             → `POST /auth/signup`
* `signInWithPassword` → `POST /auth/login`
* `signOut`            → `POST /auth/logout`
* `getSession`         → `GET  /auth/session`
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_active_user
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    SessionResponse,
    SignUpRequest,
    TokenResponse,
)
from app.schemas.common import ApiResponse
from app.schemas.user import UserOut
from app.services.auth_service import AuthService

router = APIRouter()


@router.post("/signup", response_model=ApiResponse[TokenResponse], status_code=201)
async def sign_up(
    payload: SignUpRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[TokenResponse]:
    """Register a new account (mobile `CreateAccountScreen`)."""
    tokens = await AuthService(db).sign_up(payload)
    return ApiResponse.success(data=tokens, message="Account created.")


@router.post("/login", response_model=ApiResponse[TokenResponse])
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[TokenResponse]:
    """Authenticate user and issue access + refresh tokens."""
    tokens = await AuthService(db).authenticate(payload)
    return ApiResponse.success(data=tokens)


@router.post("/refresh", response_model=ApiResponse[TokenResponse])
async def refresh_tokens(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[TokenResponse]:
    """Exchange a refresh token for a new access + refresh token pair.

    This is the only endpoint that accepts a refresh token; everywhere else
    the `type` claim is enforced to be `access`.
    """
    tokens = await AuthService(db).refresh(payload.refresh_token)
    return ApiResponse.success(data=tokens)


@router.post("/logout", response_model=ApiResponse[None])
async def logout() -> ApiResponse[None]:
    """Sign out the current session.

    Token revocation will require a denylist; for now clients should
    simply discard their copy of the token.
    """
    return ApiResponse.success(message="Signed out.")


@router.get("/session", response_model=ApiResponse[SessionResponse])
async def get_session(
    current_user: User = Depends(get_current_active_user),
) -> ApiResponse[SessionResponse]:
    """Return the user attached to the bearer token (mirrors `getSession`)."""
    return ApiResponse.success(
        data=SessionResponse(user=UserOut.model_validate(current_user))
    )


@router.get("/me", response_model=ApiResponse[UserOut])
async def get_me(
    current_user: User = Depends(get_current_active_user),
) -> ApiResponse[UserOut]:
    """Return the current user — handy for profile loads."""
    return ApiResponse.success(data=UserOut.model_validate(current_user))
