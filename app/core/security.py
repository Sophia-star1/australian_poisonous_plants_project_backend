"""JWT token management and password hashing utilities."""

from datetime import datetime


def create_access_token(data: dict[str, str | datetime], expires_delta: int | None = None) -> str:
    """Generate a signed JWT access token."""
    ...


def create_refresh_token(data: dict[str, str | datetime], expires_delta: int | None = None) -> str:
    """Generate a signed JWT refresh token."""
    ...


def verify_token(token: str) -> dict[str, object] | None:
    """Decode and validate a JWT token, returning claims or None."""
    ...


def get_password_hash(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    ...


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against its bcrypt hash."""
    ...
