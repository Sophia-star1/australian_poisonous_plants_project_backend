"""User CRUD business logic."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate


class UserService:
    """Handle user CRUD operations."""

    def __init__(self, db: AsyncSession) -> None:
        """Initialize with a database session."""
        self.db = db

    async def get_by_id(self, user_id: int) -> User | None:
        """Fetch a user by primary key."""
        ...

    async def get_by_email(self, email: str) -> User | None:
        """Fetch a user by email address."""
        ...

    async def list_users(self, page: int = 1, page_size: int = 20) -> tuple[list[User], int]:
        """Return a paginated list of users and total count."""
        ...

    async def create(self, payload: UserCreate) -> User:
        """Create a new user record."""
        ...

    async def update(self, user_id: int, payload: UserUpdate) -> User | None:
        """Update an existing user record."""
        ...

    async def delete(self, user_id: int) -> bool:
        """Soft-delete a user by ID."""
        ...
