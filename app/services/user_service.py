"""User CRUD business logic."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_password_hash
from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate


class UserService:
    """Handle user CRUD operations."""

    def __init__(self, db: AsyncSession) -> None:
        """Initialize with a database session."""
        self.db = db

    async def get_by_id(self, user_id: int) -> User | None:
        """Fetch a user by primary key."""
        result = await self.db.execute(select(User).where(User.id == user_id))
        return result.scalars().first()

    async def get_by_email(self, email: str) -> User | None:
        """Fetch a user by email address (case-insensitive)."""
        result = await self.db.execute(
            select(User).where(User.email == email.lower())
        )
        return result.scalars().first()

    async def list_users(
        self, page: int = 1, page_size: int = 20
    ) -> tuple[Sequence[User], int]:
        """Return a paginated list of users and the total count."""
        page = max(page, 1)
        page_size = max(min(page_size, 100), 1)
        offset = (page - 1) * page_size

        total_stmt = select(User)
        total_result = await self.db.execute(total_stmt)
        total = len(total_result.scalars().all())

        stmt = select(User).order_by(User.id.asc()).offset(offset).limit(page_size)
        result = await self.db.execute(stmt)
        return result.scalars().all(), total

    async def create(self, payload: UserCreate) -> User:
        """Create a new user record. Caller must check for duplicate emails."""
        user = User(
            email=payload.email.lower(),
            hashed_password=get_password_hash(payload.password),
            full_name=payload.full_name,
            role=payload.role,
            is_active=True,
        )
        self.db.add(user)
        await self.db.flush()
        await self.db.refresh(user)
        return user

    async def update(self, user_id: int, payload: UserUpdate) -> User | None:
        """Partially update an existing user record."""
        user = await self.get_by_id(user_id)
        if user is None:
            return None

        if payload.email is not None:
            user.email = payload.email.lower()
        if payload.password is not None:
            user.hashed_password = get_password_hash(payload.password)
        if payload.full_name is not None:
            user.full_name = payload.full_name
        if payload.is_active is not None:
            user.is_active = payload.is_active
        if payload.role is not None:
            user.role = payload.role

        await self.db.flush()
        await self.db.refresh(user)
        return user

    async def delete(self, user_id: int) -> bool:
        """Soft-delete a user by toggling `is_active` off."""
        user = await self.get_by_id(user_id)
        if user is None:
            return False
        user.is_active = False
        await self.db.flush()
        return True
