"""The server refuses to start with a signing key anyone can read.

`SECRET_KEY` defaults to "change-me" in `config.py`, and `.env.example` ships
another placeholder. A deployment that lost its secret binding would boot on
one and accept forged tokens for any account, an administrator's included.
"""

from __future__ import annotations

import pytest

from app import main
from app.config import settings


@pytest.mark.parametrize("value", ["change-me", "change-me-to-a-random-secret", "", "change-me\n"])
def test_a_placeholder_key_stops_a_production_start(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "SECRET_KEY", value)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        main.refuse_placeholder_secret()


def test_a_real_key_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "SECRET_KEY", "9f2c1e7a4b8d6f03a5c7e9b1d3f5a7c9")
    main.refuse_placeholder_secret()


def test_debug_still_runs_on_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh checkout with no .env must still start for development."""
    monkeypatch.setattr(settings, "DEBUG", True)
    monkeypatch.setattr(settings, "SECRET_KEY", "change-me")
    main.refuse_placeholder_secret()


@pytest.mark.asyncio
async def test_the_check_runs_in_the_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wired into startup, not just defined: uvicorn exits before serving."""
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "SECRET_KEY", "change-me")
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        async with main.lifespan(main.app):
            pass
