"""Plant data-access service.

Replaces the direct Supabase calls the mobile app used to make
(`searchPlants`, `from('plants').select(...)`). Everything the screens
need lives here:

* `list_plants` — browse list, ordered by botanical name.
* `get_plant` — full detail row (with images joined in).
* `search_plants` — ranked search across name, family, origin, lethality,
  toxic parts, toxic to animals, distribution, and health effects.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnExpressionArgument, Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.plant import Plant

SEARCH_DEFAULT_LIMIT = 25


def _escape_ilike(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _array_ilike(col: Any, like: str) -> ColumnExpressionArgument[bool]:
    """Case-insensitive substring match against any element of a text array."""
    return func.array_to_string(col, ",").ilike(like, escape="\\")


def _score_match(plant: Plant, query: str) -> int:
    """Rank a plant row against the (already-lowercased) query.

    Mirrors the original mobile client ranking and extends it to cover
    the additional searchable fields.
    """
    botanical = (plant.botanical_name or "").lower()
    family = (plant.family_name or "").lower()
    commons = [n.lower() for n in (plant.common_names or [])]
    origin = (plant.origin_status or "").lower()
    lethality = (plant.lethality or "").lower()
    toxic_parts = [n.lower() for n in (plant.toxic_parts or [])]
    toxic_animals = [n.lower() for n in (plant.toxic_to_animals or [])]
    dist_formal = [n.lower() for n in (plant.distribution_formally_listed or [])]
    dist_observed = [n.lower() for n in (plant.distribution_observed or [])]

    if botanical == query or query in commons:
        return 100
    if botanical.startswith(query):
        return 80
    if any(n.startswith(query) for n in commons):
        return 70
    if query in botanical:
        return 50
    if any(query in n for n in commons):
        return 45
    if family == query:
        return 40
    if family.startswith(query):
        return 30
    if query in family:
        return 20
    if origin == query:
        return 38
    if query in origin:
        return 18
    if lethality == query:
        return 36
    if query in lethality:
        return 16
    if any(query in n for n in toxic_parts):
        return 34
    if any(query in n for n in toxic_animals):
        return 32
    if any(query in n for n in dist_formal):
        return 28
    if any(query in n for n in dist_observed):
        return 26
    return 0


class PlantService:
    """Plant read-side operations for the mobile API."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_plants(self, limit: int | None = None) -> Sequence[Plant]:
        stmt = (
            select(Plant)
            .options(selectinload(Plant.images))
            .order_by(Plant.botanical_name.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def get_plant(self, plant_id: int) -> Plant | None:
        stmt = (
            select(Plant)
            .options(selectinload(Plant.images))
            .where(Plant.id == plant_id)
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def search_plants(self, raw_query: str, limit: int = SEARCH_DEFAULT_LIMIT) -> list[Plant]:
        """Search plants across name, family, origin, lethality, toxic parts,
        habitat, distribution, health effects, and historical records.

        Empty / whitespace-only queries resolve to `[]`.
        """
        query = raw_query.strip()
        if not query:
            return []

        safe = _escape_ilike(query)
        like = f"%{safe}%"

        stmt = (
            select(Plant)
            .options(selectinload(Plant.images))
            .where(
                or_(
                    Plant.botanical_name.ilike(like, escape="\\"),
                    Plant.family_name.ilike(like, escape="\\"),
                    _array_ilike(Plant.common_names, like),
                    Plant.origin_status.ilike(like, escape="\\"),
                    Plant.lethality.ilike(like, escape="\\"),
                    _array_ilike(Plant.toxic_parts, like),
                    _array_ilike(Plant.toxic_to_animals, like),
                    _array_ilike(Plant.distribution_formally_listed, like),
                    _array_ilike(Plant.distribution_observed, like),
                    cast(Plant.health_effects, Text).ilike(like, escape="\\"),
                )
            )
            .order_by(Plant.botanical_name.asc())
            .limit(max(1, min(limit, 200)))
        )
        result = await self.db.execute(stmt)
        rows = list(result.scalars().all())

        lowered = query.lower()
        rows.sort(key=lambda p: (-_score_match(p, lowered), (p.botanical_name or "").lower()))
        return rows
