"""Plant and PlantImage ORM models — matches Supabase `public.plants_v2`.

## Why v2

The original `plants` / `plant_images` pair is no longer the source of truth.
`plants_v2` is not a superset of it — it reorganises the toxicity data and
drops two columns — so this is a rewrite of the mapping rather than a rename:

  plants                                  plants_v2
  ---------------------------------------------------------------------
  distribution_formally_listed            distribution   (merged)
  distribution_observed                     "
  toxic_to_humans   (boolean)             toxicity_by_species (jsonb)
  toxic_to_animals  (text[])                "
  health_effects    (jsonb)               health_effects (text)
  toxin_profile     {onset,toxins,…}      {primary_class,secondary_class,…}
  morphological_attributes {leaf,flower,…} {height_m,woodiness,growth_form,…}
  photographs       (text[])              — dropped, images are relational
  historical_case_records (text)          — dropped

The old tables are deliberately left in the database. Nothing here reads
them any more, but keeping them costs nothing and is the only rollback that
does not need a restore.

## Everything is NOT NULL

Every column in `plants_v2` is NOT NULL with a `'Unknown'` (or
`ARRAY['Unknown']`, or a jsonb object of `'Unknown'`) server default. That is
a deliberate convention of the new dataset — a missing value is recorded as
the string `'Unknown'` rather than as SQL NULL — so the Python types here are
non-optional to match. Writers must supply a value for every column;
`SubmissionService._create_plant` is what fills in `'Unknown'` for whatever a
reviewer left blank.
"""

from typing import Any

from sqlalchemy import ARRAY, BigInteger, Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin

#: The value the dataset uses instead of NULL.
UNKNOWN = "Unknown"

#: The species keys `toxicity_by_species` carries, in the order the dataset
#: writes them. `notes` is free text rather than a species; it is part of the
#: same object, so it is listed here and filtered out where only species are
#: wanted.
TOXICITY_SPECIES: tuple[str, ...] = (
    "humans",
    "cattle",
    "horses",
    "sheep",
    "goats",
    "pigs",
    "poultry",
    "dogs",
    "cats",
    "other",
)

#: Stored as a value of `toxicity_by_species[species]` when the species is
#: known to be affected. Anything else — including `'Unknown'` — is not a
#: positive finding.
TOXIC = "Toxic"


def not_recorded(value: object) -> bool:
    """Whether a `plants_v2` value means "nothing was recorded here".

    Needed because v2 never stores NULL or an empty list: a gap is the string
    `'Unknown'`, `['Unknown']`, or a jsonb object whose every value is
    `'Unknown'`. So the ordinary emptiness test is not just imprecise here, it
    is inverted — `if not plant.health_effects` is False for every one of the
    89 rows that record nothing, because `'Unknown'` is a truthy string.

    Anything that carries at least one real value is considered recorded, so a
    profile naming only the humans column still counts.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip() == UNKNOWN
    if isinstance(value, dict):
        return all(not_recorded(v) for v in value.values()) if value else True
    if isinstance(value, (list, tuple)):
        return all(not_recorded(v) for v in value) if value else True
    return False


class Plant(Base, CreatedAtMixin):
    """A poisonous plant entry."""

    __tablename__ = "plants_v2"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    botanical_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    family_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    common_names: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)

    origin_status: Mapped[str] = mapped_column(Text, nullable=False)
    #: `distribution_formally_listed` and `distribution_observed` collapsed
    #: into one list. The distinction is not recoverable from v2 data.
    distribution: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    habitat: Mapped[str] = mapped_column(Text, nullable=False)
    seasonality_of_toxicity: Mapped[str] = mapped_column(Text, nullable=False)

    toxic_parts: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    #: Per-species toxicity, keyed by `TOXICITY_SPECIES` plus `notes`. Replaces
    #: the old boolean-plus-list pair, which could not express "toxic to
    #: horses, unknown for dogs".
    toxicity_by_species: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    lethality: Mapped[str] = mapped_column(Text, nullable=False)

    toxin_profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: Prose in v2, not the structured object v1 used.
    health_effects: Mapped[str] = mapped_column(Text, nullable=False)
    morphological_attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    images: Mapped[list["PlantImage"]] = relationship(
        "PlantImage",
        back_populates="plant",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def toxic_to_humans(self) -> bool:
        """Whether this plant is recorded as toxic to humans.

        Kept as a derived property because the mobile app's plant list and
        detail screens read `toxic_to_humans`, and v2 has no such column. A
        value of anything other than `'Toxic'` — including the very common
        `'Unknown'` — is not a positive finding, so this is deliberately not
        "is it not-Unknown".
        """
        return (self.toxicity_by_species or {}).get("humans") == TOXIC

    @property
    def toxic_to_animals(self) -> list[str]:
        """Species other than humans recorded as toxic, for the same reason.

        Returns the v1-shaped list the mobile app already renders, built from
        the v2 object. `notes` is excluded: it is commentary, not a species.
        """
        profile = self.toxicity_by_species or {}
        return [s for s in TOXICITY_SPECIES if s != "humans" and profile.get(s) == TOXIC]


class PlantImage(Base, CreatedAtMixin):
    """A photo attached to a plant entry.

    Note the `(plant_id, image_url)` unique constraint in the database: the
    same URL cannot be attached to one plant twice. Promotion of submission
    photos relies on each upload having a distinct object key, which
    `build_object_key` guarantees.
    """

    __tablename__ = "plant_images_v2"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("plants_v2.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    image_url: Mapped[str] = mapped_column(Text, nullable=False)
    #: New in v2. The curated rows point this at an external thumbnail
    #: service; promoted submission photos have no thumbnail, hence nullable.
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    plant: Mapped[Plant] = relationship("Plant", back_populates="images")
