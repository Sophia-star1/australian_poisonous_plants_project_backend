"""CORS middleware configuration."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings

# Dev-time helper: match http(s)://localhost:<any-port> and http://<LAN-IP>:<any-port>
# so Expo / Metro / native devices can hit the backend without hardcoding the port.
_DEV_ORIGIN_REGEX = (
    r"^https?://("
    r"localhost(:\d+)?"
    r"|127\.0\.0\.1(:\d+)?"
    r"|10\.\d+\.\d+\.\d+(:\d+)?"
    r"|192\.168\.\d+\.\d+(:\d+)?"
    r"|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+(:\d+)?"
    r")$"
)


def add_cors_middleware(app: FastAPI) -> None:
    """Attach CORS middleware with allowed origins from settings."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_origin_regex=_DEV_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
