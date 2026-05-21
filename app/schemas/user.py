"""User CRUD schemas."""

from pydantic import BaseModel, EmailStr


class UserCreate(BaseModel):
    """Schema for creating a new user."""

    email: EmailStr
    password: str
    full_name: str | None = None
    role: str = "user"


class UserUpdate(BaseModel):
    """Schema for partially updating a user."""

    email: EmailStr | None = None
    password: str | None = None
    full_name: str | None = None
    is_active: bool | None = None
    role: str | None = None


class UserOut(BaseModel):
    """Public user representation returned by the API."""

    id: int
    email: str
    full_name: str | None = None
    is_active: bool
    role: str

    model_config = {"from_attributes": True}
