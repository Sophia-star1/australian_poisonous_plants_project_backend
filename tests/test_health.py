"""Liveness stays blind to the database; readiness does not.

`/health` answered 200 throughout the month the database was down, so
nothing watching it could tell. `/health/ready` is the check that looks.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from app import main
from app.db import session as db_session


async def _down() -> None:
    raise ConnectionRefusedError("could not connect to server: password=hunter2")


@pytest.mark.asyncio
async def test_liveness_does_not_touch_the_database(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A liveness probe that failed with the database would restart healthy instances."""
    monkeypatch.setattr(db_session, "ping", _down)
    response = await async_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_ready_when_the_database_answers(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _up() -> None:
        return None

    monkeypatch.setattr(db_session, "ping", _up)
    response = await async_client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


@pytest.mark.asyncio
async def test_not_ready_when_it_does_not(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db_session, "ping", _down)
    response = await async_client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unreachable"}
    # Public endpoint: the cause is logged, never returned.
    assert "hunter2" not in response.text


@pytest.mark.asyncio
async def test_a_hanging_database_is_not_ready_either(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paused Supabase project tends to hang rather than refuse."""

    async def _hang() -> None:
        await asyncio.sleep(5)

    monkeypatch.setattr(db_session, "ping", _hang)
    monkeypatch.setattr(main, "READINESS_TIMEOUT_SECONDS", 0.05)
    response = await async_client.get("/health/ready")
    assert response.status_code == 503
