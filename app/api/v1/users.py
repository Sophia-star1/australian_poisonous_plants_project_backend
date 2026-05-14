"""User CRUD endpoints."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.dependencies import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse, PageResult
from app.schemas.user import UserCreate, UserOut, UserUpdate

router = APIRouter()


@router.get("/", response_model=ApiResponse[PageResult[UserOut]])
async def list_users(
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResult[UserOut]]:
    """Return a paginated list of users."""
    ...


@router.post("/", response_model=ApiResponse[UserOut], status_code=201)
async def create_user(
    payload: UserCreate,
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[UserOut]:
    """Register a new user."""
    ...


@router.get("/{user_id}", response_model=ApiResponse[UserOut])
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_active_user),
) -> ApiResponse[UserOut]:
    """Retrieve a single user by ID."""
    ...


@router.patch("/{user_id}", response_model=ApiResponse[UserOut])
async def update_user(
    user_id: int,
    payload: UserUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_active_user),
) -> ApiResponse[UserOut]:
    """Partially update a user's profile."""
    ...


@router.delete("/{user_id}", response_model=ApiResponse[None])
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_active_user),
) -> ApiResponse[None]:
    """Soft-delete a user by ID."""
    ...
