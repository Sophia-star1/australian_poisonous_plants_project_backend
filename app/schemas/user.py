"""User CRUD schemas."""

from pydantic import BaseModel, EmailStr

from app.schemas.role import DEFAULT_ROLE, UserRole


class UserCreate(BaseModel):
    """Schema for creating a new user."""

    email: EmailStr
    password: str
    full_name: str | None = None
    role: UserRole = DEFAULT_ROLE


class UserUpdate(BaseModel):
    """Schema for partially updating a user."""

    email: EmailStr | None = None
    password: str | None = None
    full_name: str | None = None
    is_active: bool | None = None
    role: UserRole | None = None


class UserRoleUpdate(BaseModel):
    """Admin action: change a single user's role."""

    role: UserRole


class UserOut(BaseModel):
    """Public user representation returned by the API."""

    id: int
    email: str
    full_name: str | None = None
    is_active: bool
    role: str
    #: A 512x512 JPEG, or null when the user has not set one.
    avatar_url: str | None = None

    model_config = {"from_attributes": True}
