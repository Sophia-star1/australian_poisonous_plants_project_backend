"""CORS configuration tests.

The private-network regex used to apply unconditionally, so the production
API accepted credentialed cross-origin requests from any page served on
localhost or a private address.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.middleware.cors_middleware import add_cors_middleware


def _app_with_cors(debug: bool, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Build a throwaway app whose CORS reflects the given DEBUG value."""
    monkeypatch.setattr(settings, "DEBUG", debug)
    app = FastAPI()
    add_cors_middleware(app)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "1"}

    return app


async def _origin_allowed(app: FastAPI, origin: str) -> bool:
    """Send a preflight and report whether the origin was echoed back."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.options(
            "/ping",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )
    return response.headers.get("access-control-allow-origin") == origin


@pytest.mark.asyncio
async def test_lan_origin_allowed_in_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """A phone on the same Wi-Fi must still reach a dev laptop."""
    app = _app_with_cors(debug=True, monkeypatch=monkeypatch)
    assert await _origin_allowed(app, "http://192.168.1.7:8081")
    assert await _origin_allowed(app, "http://localhost:19006")


@pytest.mark.asyncio
async def test_lan_origin_rejected_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the private-network regex used to be live in production."""
    app = _app_with_cors(debug=False, monkeypatch=monkeypatch)
    assert not await _origin_allowed(app, "http://192.168.1.7:8081")
    assert not await _origin_allowed(app, "http://10.0.0.5:3000")
    assert not await _origin_allowed(app, "http://127.0.0.1:5173")


@pytest.mark.asyncio
async def test_configured_origins_still_allowed_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ALLOWED_ORIGINS is the only list that applies once DEBUG is off."""
    monkeypatch.setattr(settings, "ALLOWED_ORIGINS", ["https://app.example.org"])
    app = _app_with_cors(debug=False, monkeypatch=monkeypatch)
    assert await _origin_allowed(app, "https://app.example.org")


@pytest.mark.asyncio
async def test_unrelated_origin_always_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public origin outside the list is refused in either mode."""
    for debug in (True, False):
        app = _app_with_cors(debug=debug, monkeypatch=monkeypatch)
        assert not await _origin_allowed(app, "https://evil.example.com")
