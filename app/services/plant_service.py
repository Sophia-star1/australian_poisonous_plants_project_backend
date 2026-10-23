"""Plant data-access service.

Everything the mobile screens need lives here:

* `list_plants` — browse list, ordered by botanical name.
* `count_plants` — how many rows that list holds, counted in SQL.
* `get_plant` — full detail row (with images joined in).
* `search_plants` — ranked search across name, family, origin, lethality,
  toxic parts, distribution, health effects, the toxicity profile and the
  toxin profile. Supports optional field-scoped search via `fields`.

And the two admin writes: `update_plant` corrects an entry, `delete_plant`
removes one.

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

from app.config import settings
from app.db.like import contains_pattern
from app.models.plant import TOXIC, TOXICITY_SPECIES, Plant, unknown_defaults
from app.schemas.plant import SEARCHABLE_FIELDS
from app.schemas.submission import PlantUpdate
from app.services.storage_service import StorageService

#: The jsonb columns `update_plant` merges key by key rather than replaces.
_MERGED_OBJECTS = ("toxicity_by_species", "toxin_profile", "morphological_attributes")

SEARCH_DEFAULT_LIMIT = 25


class DuplicatePlantError(ValueError):
    """A plant with this botanical name is already in the library."""

    def __init__(self, name: str, existing_id: int) -> None:
        super().__init__(
            f"A plant named '{name}' already exists (id {existing_id}). Add to it "
            "with plant_id instead of publishing a second entry."
        )
        self.existing_id = existing_id


def _comparable_name(column: Any) -> Any:
    """`column` trimmed, inner whitespace collapsed, lowercased — in SQL."""
    return func.lower(func.regexp_replace(func.btrim(column), r"\s+", " ", "g"))


async def find_plant_named(
    db: AsyncSession, name: str, *, exclude_id: int | None = None
) -> int | None:
    """The id of a plant already called `name`, ignoring case and spacing.

    `plants_v2` has no unique constraint on the name, so this check is what
    stops a second "Abrus precatorius" being published. Two requests creating
    the same new name at the same instant can still both pass it; adding the
    constraint would close that, at the cost of a schema change.
    """
    wanted = " ".join(name.split()).lower()
    stmt = select(Plant.id).where(_comparable_name(Plant.botanical_name) == wanted)
    if exclude_id is not None:
        stmt = stmt.where(Plant.id != exclude_id)
    found = await db.scalar(stmt.order_by(Plant.id).limit(1))
    return int(found) if found is not None else None


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
    return (
        select(1)
        .select_from(kv)
        .where(
            or_(
                and_(kv.c.value == TOXIC, kv.c.key.ilike(like, escape="\\")),
                and_(kv.c.key == "notes", kv.c.value.ilike(like, escape="\\")),
            )
        )
        .exists()
    )


def _toxin_profile_ilike(like: str) -> ColumnExpressionArgument[bool]:
    """Match the *values* of `toxin_profile`, never its keys.

    Matching keys would make "class" return every row, because every row has
    `primary_class` and `secondary_class`.
    """
    kv = func.jsonb_each_text(Plant.toxin_profile).table_valued("key", "value").alias("prof")
    return select(1).select_from(kv).where(kv.c.value.ilike(like, escape="\\")).exists()


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
            select(Plant).options(selectinload(Plant.images)).order_by(Plant.botanical_name.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.db.execute(stmt)
        return result.scalars().all()

    async def count_plants(self) -> int:
        """The number of plants `list_plants` returns when given no limit."""
        total = await self.db.scalar(select(func.count()).select_from(Plant))
        return int(total or 0)

    async def update_plant(self, plant_id: int, changes: PlantUpdate) -> Plant | None:
        """Apply an admin correction; `None` if there is no such plant.

        Only the fields the request sent are touched (`exclude_unset`). A field
        sent blank arrives as `None` and goes back to the dataset's default,
        `'Unknown'`, the same value an approval fills a gap with. The jsonb
        objects are merged key by key, so correcting one species does not wipe
        the other ten. A rename to a name another plant already has is
        refused, as an approval would be.
        """
        plant = await self.db.get(Plant, plant_id)
        if plant is None:
            return None

        sent = changes.model_dump(exclude_unset=True)
        defaults = unknown_defaults()

        name = sent.pop("botanical_name", None)
        if name is not None:
            duplicate = await find_plant_named(self.db, name, exclude_id=plant_id)
            if duplicate is not None:
                raise DuplicatePlantError(name, duplicate)
            plant.botanical_name = name

        for field, value in sent.items():
            if field in _MERGED_OBJECTS and value is not None:
                merged = dict(getattr(plant, field) or {})
                for key, entry in value.items():
                    if entry is not None:
                        merged[key] = entry
                    elif key in defaults[field]:
                        merged[key] = defaults[field][key]
                    else:
                        # A key the dataset does not define (morphology is
                        # open): clearing it removes it.
                        merged.pop(key, None)
                # A new object, not an in-place edit: SQLAlchemy only notices
                # a jsonb change when the attribute is reassigned.
                setattr(plant, field, merged)
            else:
                setattr(plant, field, value if value is not None else defaults[field])

        await self.db.flush()
        return plant

    async def delete_plant(self, plant_id: int) -> list[str] | None:
        """Delete a plant and its image rows; `None` if there is no such plant.

        Returns the image URLs the rows held, so the caller can remove the
        files once the deletion has committed — before that, a rollback would
        leave rows pointing at photos that are gone.
        """
        plant = await self.db.get(Plant, plant_id)
        if plant is None:
            return None
        urls = [image.image_url for image in plant.images]
        # `Plant.images` cascades, so the image rows go with it.
        await self.db.delete(plant)
        await self.db.flush()
        return urls

    async def delete_photo_files(self, urls: list[str]) -> list[str]:
        """Remove the files behind `urls`; returns the ones left in place.

        Only files in this project's plant bucket are touched. Anything else —
        another bucket, another host — is reported back rather than guessed at.
        Never raises: the database change it cleans up after is already done.
        """
        storage = StorageService()
        kept: list[str] = []
        for url in urls:
            key = storage.key_in_bucket(url, settings.SUPABASE_PLANT_BUCKET)
            deleted = key is not None and await storage.delete_file(
                key, bucket=settings.SUPABASE_PLANT_BUCKET
            )
            if not deleted:
                kept.append(url)
        return kept

    async def get_plant(self, plant_id: int) -> Plant | None:
        stmt = select(Plant).options(selectinload(Plant.images)).where(Plant.id == plant_id)
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


__all__ = [
    "DuplicatePlantError",
    "PlantService",
    "SEARCH_DEFAULT_LIMIT",
    "SEARCHABLE_FIELDS",
    "find_plant_named",
]
