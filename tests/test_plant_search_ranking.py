"""Search ranks every match before the limit applies.

Regression: the SQL query took the alphabetically first `limit` matches and
ranked only those, so a broad query lost its best hits. In production, "an"
matched 84 plants and the five whose names start with "An" were all beyond the
first 25 alphabetically, so the app never showed them.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.models.plant import UNKNOWN, Plant
from app.services.plant_service import PlantService


def _plant(pid: int, botanical: str, distribution: list[str]) -> Plant:
    return Plant(
        id=pid,
        botanical_name=botanical,
        family_name="Fabaceae",
        common_names=[UNKNOWN],
        origin_status="Native",
        lethality=UNKNOWN,
        toxic_parts=[UNKNOWN],
        distribution=distribution,
        toxicity_by_species={},
    )


class _Session:
    """Returns the rows the way the database would: alphabetical, all of them."""

    def __init__(self, rows: list[Plant]) -> None:
        self.rows = sorted(rows, key=lambda p: p.botanical_name)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        rows = self.rows

        class _Result:
            def scalars(self) -> Any:
                class _Scalars:
                    def all(self) -> list[Plant]:
                        return rows

                return _Scalars()

        return _Result()


def _library() -> list[Plant]:
    # 29 weak matches ("an" only inside a distribution entry: "Queensland"),
    # all alphabetically ahead of the one strong match.
    weak = [_plant(i, f"Acacia sp{i:02d}", ["Queensland"]) for i in range(29)]
    strong = _plant(99, "Zanthoxylum anomalum", ["NSW"])  # "an" inside the name
    return [*weak, strong]


@pytest.mark.asyncio
async def test_the_best_match_survives_the_limit() -> None:
    service = PlantService(_Session(_library()))  # type: ignore[arg-type]

    results = await service.search_plants("an", limit=25)

    assert len(results) == 25
    assert results[0].botanical_name == "Zanthoxylum anomalum", "ranked first, not cut off"


@pytest.mark.asyncio
async def test_the_limit_is_applied_after_ranking_not_in_sql() -> None:
    session = _Session(_library())
    await PlantService(session).search_plants("an", limit=5)  # type: ignore[arg-type]

    (statement,) = session.statements
    sql = str(statement.compile(dialect=postgresql.dialect())).upper()
    assert " LIMIT " not in sql


@pytest.mark.asyncio
async def test_limit_is_still_capped() -> None:
    rows = [_plant(i, f"Acacia sp{i:03d}", ["Queensland"]) for i in range(250)]
    results = await PlantService(_Session(rows)).search_plants("an", limit=1000)  # type: ignore[arg-type]
    assert len(results) == 200
