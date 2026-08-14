"""Authentication endpoints: signup, login, logout, session.

Replaces the mobile app's direct `supabase.auth.*` calls:

* `signUp`             → `POST /auth/signup`
* `signInWithPassword` → `POST /auth/login`
* `signOut`            → `POST /auth/logout`
* `getSession`         → `GET  /auth/session`
"""

import hashlib

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import TooManyRequestsError
from app.core.rate_limit import get_rate_limiter
from app.core.security import ACCESS_TOKEN_TYPE, REFRESH_TOKEN_TYPE, verify_token
from app.core.token_denylist import get_denylist, seconds_until
from app.db.session import get_db
from app.dependencies import get_current_active_user, oauth2_scheme
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    SessionResponse,
    SignUpRequest,
    TokenResponse,
)
from app.schemas.common import ApiResponse
from app.schemas.user import UserOut
from app.services.auth_service import AuthService

router = APIRouter()


def _client_ip(request: Request) -> str:
    """Best-effort client IP.

    Cloud Run terminates TLS at its front end, so `request.client.host` is the
    load balancer. The first entry of X-Forwarded-For is the original caller.
    It is spoofable in general, which is why the account-scoped limit below
    exists alongside the IP-scoped one — an attacker can rotate one or the
    other, but not both at once.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _account_key(email: str) -> str:
    """Hash the email so credential material never lands in Redis or logs."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:32]


async def _enforce_rate_limit(scope: str, identifiers: list[str], limit: int) -> None:
    """Raise 429 if any identifier has exhausted its budget for this window."""
    if not settings.RATE_LIMIT_ENABLED:
        return
    limiter = get_rate_limiter()
    for identifier in identifiers:
        result = await limiter.hit(
            f"rl:{scope}:{identifier}", limit, settings.RATE_LIMIT_WINDOW_SECONDS
        )
        if not result.allowed:
            raise TooManyRequestsError(
                "Too many attempts. Please wait before trying again.",
                retry_after=result.retry_after,
            )


@router.post("/signup", response_model=ApiResponse[TokenResponse], status_code=201)
async def sign_up(
    request: Request,
    payload: SignUpRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[TokenResponse]:
    """Register a new account (mobile `CreateAccountScreen`)."""
    await _enforce_rate_limit(
        "signup", [f"ip:{_client_ip(request)}"], settings.SIGNUP_RATE_LIMIT
    )
    tokens = await AuthService(db).sign_up(payload)
    return ApiResponse.success(data=tokens, message="Account created.")


@router.post("/login", response_model=ApiResponse[TokenResponse])
async def login(
    request: Request,
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[TokenResponse]:
    """Authenticate user and issue access + refresh tokens."""
    ip_key = f"ip:{_client_ip(request)}"
    account_key = f"acct:{_account_key(payload.email)}"
    await _enforce_rate_limit("login", [ip_key, account_key], settings.LOGIN_RATE_LIMIT)

    tokens = await AuthService(db).authenticate(payload)

    # Successful sign-in clears the account counter so a user who mistyped a
    # few times then got it right is not left throttled.
    if settings.RATE_LIMIT_ENABLED:
        await get_rate_limiter().reset(f"rl:login:{account_key}")
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
async def logout(
    payload: LogoutRequest | None = None,
    token: str | None = Depends(oauth2_scheme),
) -> ApiResponse[None]:
    """Sign out: revoke the presented access token and, if given, the refresh token.

    Clients should send the refresh token in the body as well. Revoking only
    the access token would leave the refresh token able to mint a new one,
    which is the same as not logging out at all.

    Always reports success — an expired or malformed token is already
    unusable, and reporting otherwise would tell a caller which tokens are
    valid.
    """
    denylist = get_denylist()

    for candidate, expected_type in (
        (token, ACCESS_TOKEN_TYPE),
        (payload.refresh_token if payload else None, REFRESH_TOKEN_TYPE),
    ):
        if not candidate:
            continue
        claims = verify_token(candidate, expected_type=expected_type)
        if claims is None:
            continue
        jti = claims.get("jti")
        if isinstance(jti, str):
            await denylist.revoke(jti, seconds_until(claims.get("exp")))

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
