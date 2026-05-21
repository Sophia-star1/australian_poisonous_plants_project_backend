"""Plant data-access service.

Replaces the direct Supabase calls the mobile app used to make
(`searchPlants`, `from('plants').select(...)`). Everything the screens
need lives here:

* `list_plants` — browse list, ordered by botanical name.
* `get_plant` — full detail row (with images joined in).
* `search_plants` — ranked search across botanical/common/family names.
"""

from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.plant import Plant

SEARCH_DEFAULT_LIMIT = 25


def _escape_ilike(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _score_match(plant: Plant, query: str) -> int:
    """Mirror the ranking from mobile/.../api/plants.ts::scoreMatch."""
    botanical = (plant.botanical_name or "").lower()
    family = (plant.family_name or "").lower()
    commons = [n.lower() for n in (plant.common_names or [])]
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
        """Search plants by botanical name, common name, or family.

        Empty / whitespace-only queries resolve to `[]` to match the
        original mobile helper.
        """
        query = raw_query.strip()
        if not query:
            return []

        safe = _escape_ilike(query)
        like = f"%{safe}%"
        # Flatten the common_names array so we can do a case-insensitive
        # substring match against any element.
        commons_joined = func.array_to_string(Plant.common_names, ",")
        stmt = (
            select(Plant)
            .options(selectinload(Plant.images))
            .where(
                or_(
                    Plant.botanical_name.ilike(like, escape="\\"),
                    Plant.family_name.ilike(like, escape="\\"),
                    commons_joined.ilike(like, escape="\\"),
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
