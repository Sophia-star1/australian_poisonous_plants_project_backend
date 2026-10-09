"""Plant data-access service.

Everything the mobile screens need lives here:

* `list_plants` — browse list, ordered by botanical name.
* `count_plants` — how many rows that list holds, counted in SQL.
* `get_plant` — full detail row (with images joined in).
* `search_plants` — ranked search across name, family, origin, lethality,
  toxic parts, distribution, health effects, the toxicity profile and the
  toxin profile. Supports optional field-scoped search via `fields`.

## Searching the jsonb columns is not a text cast

`toxicity_by_species` and `toxin_profile` are jsonb, and the obvious
implementation — cast the whole column to text and ILIKE it — is wrong here
in a way that is worth spelling out, because it looks like it works.

Every row carries every species key and the dataset writes `'Unknown'` rather
than omitting anything. So `cast(toxicity_by_species AS text)` contains the
literal strings `dogs`, `cattle`, `humans` and `Unknown` in all 100 rows. A
search for "dogs" would match the entire table, and so would a search for
"Unknown" — 1,056 of the 1,100 species entries hold that value. The filter
would return everything and rank nothing.

So both columns are searched by unnesting them:

* `toxicity_by_species` matches a species **name** only where that species'
  value is `'Toxic'`. Searching "dogs" means "plants toxic to dogs", which is
  the only reading a user intends. Its free-text `notes` entry is matched on
  content like any other prose field.
* `toxin_profile` matches on **values** only, never keys, so "class" does not
  match all 100 rows via `primary_class` / `secondary_class`.
"""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnExpressionArgument, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.like import contains_pattern
from app.models.plant import TOXIC, TOXICITY_SPECIES, Plant
from app.schemas.plant import SEARCHABLE_FIELDS

SEARCH_DEFAULT_LIMIT = 25


def _array_ilike(col: Any, like: str) -> ColumnExpressionArgument[bool]:
    """Case-insensitive substring match against any element of a text array."""
    return func.array_to_string(col, ",").ilike(like, escape="\\")


def _toxicity_ilike(like: str) -> ColumnExpressionArgument[bool]:
    """Match a species the plant is toxic to, or its free-text notes.

    Unnests `toxicity_by_species` and requires `value = 'Toxic'` before
    matching the key, so a species name only matches rows that actually record
    it as toxic. `notes` is matched on its value instead, since it is prose.
    """
    kv = func.jsonb_each_text(Plant.toxicity_by_species).table_valued("key", "value").alias("tox")
    return select(1).select_from(kv).where(
        or_(
            and_(kv.c.value == TOXIC, kv.c.key.ilike(like, escape="\\")),
            and_(kv.c.key == "notes", kv.c.value.ilike(like, escape="\\")),
        )
    ).exists()


def _toxin_profile_ilike(like: str) -> ColumnExpressionArgument[bool]:
    """Match the *values* of `toxin_profile`, never its keys.

    Matching keys would make "class" return every row, because every row has
    `primary_class` and `secondary_class`.
    """
    kv = func.jsonb_each_text(Plant.toxin_profile).table_valued("key", "value").alias("prof")
    return (
        select(1).select_from(kv).where(kv.c.value.ilike(like, escape="\\")).exists()
    )


def _build_search_filters(like: str) -> dict[str, ColumnExpressionArgument[bool]]:
    """Map every searchable field name to its ILIKE filter expression."""
    return {
        "botanical_name": Plant.botanical_name.ilike(like, escape="\\"),
        "family_name": Plant.family_name.ilike(like, escape="\\"),
        "common_names": _array_ilike(Plant.common_names, like),
        "origin_status": Plant.origin_status.ilike(like, escape="\\"),
        "lethality": Plant.lethality.ilike(like, escape="\\"),
        "toxic_parts": _array_ilike(Plant.toxic_parts, like),
        "distribution": _array_ilike(Plant.distribution, like),
        # Prose in v2, so no cast is needed any more.
        "health_effects": Plant.health_effects.ilike(like, escape="\\"),
        "toxicity_by_species": _toxicity_ilike(like),
        "toxin_profile": _toxin_profile_ilike(like),
    }


def _score_match(plant: Plant, query: str) -> int:
    """Rank a plant row against the (already-lowercased) query.

    Mirrors the original mobile client ranking and extends it to cover the
    v2 fields. Name matches dominate; the descriptive fields break ties.
    """
    botanical = (plant.botanical_name or "").lower()
    family = (plant.family_name or "").lower()
    commons = [n.lower() for n in (plant.common_names or [])]
    origin = (plant.origin_status or "").lower()
    lethality = (plant.lethality or "").lower()
    toxic_parts = [n.lower() for n in (plant.toxic_parts or [])]
    distribution = [n.lower() for n in (plant.distribution or [])]
    profile = plant.toxicity_by_species or {}
    toxic_species = [s for s in TOXICITY_SPECIES if profile.get(s) == TOXIC]

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
    # A species this plant is actually toxic to, e.g. "dogs".
    if any(query in s for s in toxic_species):
        return 32
    if any(query in n for n in distribution):
        return 28
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

    async def count_plants(self) -> int:
        """The number of plants `list_plants` returns when given no limit."""
        total = await self.db.scalar(select(func.count()).select_from(Plant))
        return int(total or 0)

    async def get_plant(self, plant_id: int) -> Plant | None:
        stmt = (
            select(Plant)
            .options(selectinload(Plant.images))
            .where(Plant.id == plant_id)
        )
        result = await self.db.execute(stmt)
        return result.scalars().first()

    async def search_plants(
        self,
        raw_query: str,
        limit: int = SEARCH_DEFAULT_LIMIT,
        fields: list[str] | None = None,
    ) -> list[Plant]:
        """Search plants across one or more named fields, or all fields by default.

        * `fields=None` — search every searchable field (current behaviour).
        * `fields=["botanical_name", "common_names"]` — restrict filters to
          only those columns, keeping results focused.

        Invalid field names are silently dropped. Empty / whitespace-only
        queries resolve to `[]`.

        Every match is ranked before `limit` applies. Cutting in SQL first
        kept the alphabetically first `limit` matches and ranked only those,
        so a broad query lost its best hits: typing "an" returned none of the
        five plants whose names start with "An". The table holds about a
        hundred rows, so loading every match costs little; ranking in SQL
        would mean reimplementing `_score_match` there.
        """
        query = raw_query.strip()
        if not query:
            return []

        like = contains_pattern(query)

        all_filters = _build_search_filters(like)

        if fields is None:
            selected = list(all_filters.values())
        else:
            selected = [all_filters[f] for f in fields if f in all_filters]
            if not selected:
                return []

        stmt = select(Plant).options(selectinload(Plant.images)).where(or_(*selected))
        result = await self.db.execute(stmt)
        rows = list(result.scalars().all())

        lowered = query.lower()
        rows.sort(key=lambda p: (-_score_match(p, lowered), (p.botanical_name or "").lower()))
        return rows[: max(1, min(limit, 200))]


__all__ = ["PlantService", "SEARCH_DEFAULT_LIMIT", "SEARCHABLE_FIELDS"]
