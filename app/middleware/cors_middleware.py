"""CORS middleware configuration."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Dev-time helper: match http(s)://localhost:<any-port> and http://<LAN-IP>:<any-port>
# so Expo / Metro / native devices can hit the backend without hardcoding the port.
#
# This is deliberately gated on DEBUG. Left on in production it allows any page
# served from localhost or a private address to make credentialed cross-origin
# requests to the live API — an attacker only needs the victim to visit a page
# that their own machine is serving.
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
    """Attach CORS middleware with allowed origins from settings.

    In debug builds the private-network regex is added so a phone on the same
    Wi-Fi can reach a laptop. In production only `ALLOWED_ORIGINS` applies.
    """
    origin_regex = _DEV_ORIGIN_REGEX if settings.DEBUG else None

    logger.info(
        "cors_configured",
        allowed_origins=settings.ALLOWED_ORIGINS,
        dev_origin_regex_enabled=origin_regex is not None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS,
        allow_origin_regex=origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
