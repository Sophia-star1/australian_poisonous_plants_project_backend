"""Authentication endpoints: login, refresh, logout."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.auth import LoginRequest, RefreshRequest, TokenResponse
from app.schemas.common import ApiResponse

router = APIRouter()


@router.post("/login", response_model=ApiResponse[TokenResponse])
async def login(
    payload: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[TokenResponse]:
    """Authenticate user and issue access + refresh tokens."""
    ...


@router.post("/refresh", response_model=ApiResponse[TokenResponse])
async def refresh_token(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[TokenResponse]:
    """Issue a new access token using a valid refresh token."""
    ...


@router.post("/logout", response_model=ApiResponse[None])
async def logout(
    payload: RefreshRequest,
) -> ApiResponse[None]:
    """Invalidate the provided refresh token."""
    ...
