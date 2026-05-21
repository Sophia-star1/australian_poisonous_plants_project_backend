"""Plant request/response schemas.

These mirror the row shapes the mobile app expected from Supabase so
the React Native code can be repointed at this backend with minimal
type changes.
"""

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class PlantImageOut(BaseModel):
    """Compact thumbnail row joined into plant payloads."""

    model_config = ConfigDict(from_attributes=True)

    image_url: str
    caption: str | None = None
    is_primary: bool = False


class PlantSummary(BaseModel):
    """Browse/search row used by the Plants list screen.

    The output key is `plant_images` to match what the mobile app
    receives from Supabase, but it's sourced from the ORM relationship
    named `images` via a validation alias.
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    botanical_name: str
    family_name: str | None = None
    common_names: list[str] | None = None
    origin_status: str | None = None
    lethality: str | None = None
    toxic_parts: list[str] | None = None
    toxic_to_humans: bool | None = None
    health_effects: dict[str, Any] | None = None
    plant_images: list[PlantImageOut] = Field(
        default_factory=list,
        validation_alias=AliasChoices("plant_images", "images"),
    )


class PlantDetail(PlantSummary):
    """Full row served by the Plant Detail screen."""

    distribution_formally_listed: list[str] | None = None
    distribution_observed: list[str] | None = None
    habitat: str | None = None
    seasonality_of_toxicity: str | None = None
    toxic_to_animals: list[str] | None = None
    toxin_profile: dict[str, Any] | None = None
    morphological_attributes: dict[str, Any] | None = None
    historical_case_records: str | None = None


class PlantSearchQuery(BaseModel):
    """Query-string container documenting the search endpoint."""

    q: str = Field("", description="Free-text query string.")
    limit: int = Field(25, ge=1, le=100, description="Max rows returned.")
