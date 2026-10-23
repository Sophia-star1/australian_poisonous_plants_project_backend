"""Tests for `GET /plants/count`.

Two things can go wrong here that a happy-path test would not show: the route
being shadowed by `/{plant_id}`, which answers 422 because "count" is not an
integer, and the count being taken over something other than the table the
browse list reads.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.dialects import postgresql

from app.db.session import get_db
from app.main import app


class _CountingSession:
    """Answers the one scalar query the endpoint makes, and keeps it."""

    def __init__(self, total: int | None) -> None:
        self.total = total
        self.statements: list[Any] = []

    async def scalar(self, statement: Any) -> int | None:
        self.statements.append(statement)
        return self.total


@pytest.fixture
def session() -> Any:
    def _apply(total: int | None) -> _CountingSession:
        fake = _CountingSession(total)

        async def _fake_db() -> AsyncIterator[_CountingSession]:
            yield fake

        app.dependency_overrides[get_db] = _fake_db
        return fake

    yield _apply
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_count_returns_the_number_of_plants(async_client: AsyncClient, session: Any) -> None:
    session(100)

    response = await async_client.get("/api/v1/plants/count")

    assert response.status_code == 200
    assert response.json()["data"] == {"total": 100}


@pytest.mark.asyncio
async def test_count_is_not_taken_for_a_plant_id(async_client: AsyncClient, session: Any) -> None:
    """Regression guard: declared after `/{plant_id}` this would be a 422."""
    fake = session(3)

    response = await async_client.get("/api/v1/plants/count")

    assert response.status_code == 200, response.text
    assert len(fake.statements) == 1


@pytest.mark.asyncio
async def test_count_is_one_query_over_the_browse_table(
    async_client: AsyncClient, session: Any
) -> None:
    """Counted by the database, over `plants_v2`, not by loading rows."""
    fake = session(7)

    await async_client.get("/api/v1/plants/count")

    (statement,) = fake.statements
    sql = str(statement.compile(dialect=postgresql.dialect())).lower()
    assert "count(*)" in sql
    assert "from plants_v2" in sql


@pytest.mark.asyncio
async def test_an_empty_library_counts_zero(async_client: AsyncClient, session: Any) -> None:
    session(None)

    response = await async_client.get("/api/v1/plants/count")

    assert response.json()["data"] == {"total": 0}
