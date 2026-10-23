"""Plant request/response schemas.

These follow `plants_v2`, with one deliberate exception: `toxic_to_humans` and
`toxic_to_animals` are still served, computed from `toxicity_by_species`. They
are not columns any more, but the mobile app's plant list and detail screens
read both, and breaking those screens is not part of moving tables. New
clients should prefer `toxicity_by_species`, which says which of ten species
an entry actually covers instead of collapsing the answer to a boolean.
"""

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

SearchableField = Literal[
    "botanical_name",
    "family_name",
    "common_names",
    "origin_status",
    "lethality",
    "toxic_parts",
    "distribution",
    "health_effects",
    "toxicity_by_species",
    "toxin_profile",
]

SEARCHABLE_FIELDS: set[str] = {
    "botanical_name",
    "family_name",
    "common_names",
    "origin_status",
    "lethality",
    "toxic_parts",
    "distribution",
    "health_effects",
    "toxicity_by_species",
    "toxin_profile",
}


class PlantImageOut(BaseModel):
    """One image row joined into a plant payload.

    Ordered primary-first by the `Plant.images` relationship, so
    `plant_images[0]` is always the cover.
    """

    model_config = ConfigDict(from_attributes=True)

    #: Needed to refer to one specific photo — setting a different cover,
    #: removing a photo, or just a stable React key. `SubmissionImageOut` has
    #: always exposed its id; this one did not, which meant a client could
    #: only address a plant's photos by list position or by URL.
    id: int
    image_url: str
    #: New in v2; null for photos promoted from a contributor submission,
    #: which have no separately generated thumbnail.
    thumbnail_url: str | None = None
    caption: str | None = None
    is_primary: bool = False


class PlantSummary(BaseModel):
    """Browse/search row used by the Plants list screen.

    The output key is `plant_images` to match what the mobile app already
    consumes, sourced from the ORM relationship named `images` via a
    validation alias.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    botanical_name: str
    family_name: str
    common_names: list[str]
    origin_status: str
    lethality: str
    toxic_parts: list[str]
    #: Prose in v2. Was a structured object in v1; clients that indexed into
    #: it (`health_effects.symptoms`) need updating.
    health_effects: str
    #: Per-species toxicity: the ten species keys plus free-text `notes`.
    toxicity_by_species: dict[str, Any]
    #: Derived, not stored — see the module docstring.
    toxic_to_humans: bool
    plant_images: list[PlantImageOut] = Field(
        default_factory=list,
        validation_alias=AliasChoices("plant_images", "images"),
    )


class PlantDetail(PlantSummary):
    """Full row served by the Plant Detail screen."""

    #: `distribution_formally_listed` and `distribution_observed` merged; the
    #: two are not distinguishable in v2 data.
    distribution: list[str]
    habitat: str
    seasonality_of_toxicity: str
    toxin_profile: dict[str, Any]
    morphological_attributes: dict[str, Any]
    #: Derived, not stored. Species other than humans recorded as toxic.
    toxic_to_animals: list[str]


class PlantCount(BaseModel):
    """Served by `GET /plants/count`."""

    total: int = Field(description="Number of plants in the library.")


class PlantDeleted(BaseModel):
    """Served by `DELETE /plants/{id}`."""

    id: int
    #: Photos removed from this project's plant bucket.
    photos_deleted: int
    #: Image URLs not removed: hosted elsewhere, or a storage delete that
    #: failed (logged). Their rows are gone either way.
    photos_not_deleted: list[str] = Field(default_factory=list)


class PlantSearchQuery(BaseModel):
    """Query-string container documenting the search endpoint."""

    q: str = Field("", description="Free-text query string.")
    limit: int = Field(25, ge=1, le=100, description="Max rows returned.")
