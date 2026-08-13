"""Submission request/response schemas — match real Supabase schema."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

SubmissionStatus = Literal["pending", "approved", "rejected"]
ReviewAction = Literal["approved", "rejected"]

#: The only status from which a review decision may be made.
PENDING_STATUS = "pending"


class SubmissionImageOut(BaseModel):
    """One image row joined into a submission."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    image_url: str
    is_primary: bool = False


class SubmissionCreate(BaseModel):
    """Non-file fields accepted by `POST /submissions/`."""

    user_description: str | None = None
    contributor_name: str = Field(..., min_length=1, max_length=255)
    contributor_email: EmailStr
    location_state: str | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None


class SubmissionOut(BaseModel):
    """Submission row returned by list / detail endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    contributor_name: str
    contributor_email: str
    user_description: str | None = None
    location_state: str | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    status: SubmissionStatus
    ai_is_plant: bool | None = None
    ai_top_matches: dict[str, Any] | None = None
    reviewer_notes: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    submitted_at: datetime
    images: list[SubmissionImageOut] = Field(default_factory=list)


class SubmissionReviewResult(BaseModel):
    """What a review returns: the updated submission plus what it produced."""

    submission: "SubmissionOut"
    #: Id of the plant created by an approval; None for a rejection.
    plant_id: int | None = None
    #: Non-blocking notes about gaps in the published entry.
    warnings: list[str] = Field(default_factory=list)


class SubmissionFilter(BaseModel):
    """Query-string filters for the admin submissions list."""

    status: SubmissionStatus | None = None
    contributor_name: str | None = None
    contributor_email: str | None = None
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)


class PlantIdentification(BaseModel):
    """The botanical record a reviewer supplies when approving a submission.

    A submission carries a photo, a description and a location — none of the
    fields `plants` needs. `botanical_name` is the only NOT NULL column and
    has no counterpart in `submissions`, so approval cannot be a mechanical
    copy: the identification and the toxicity profile come from the reviewer.

    Only `botanical_name` is required. The toxicity fields are optional so a
    reviewer is not blocked mid-review, but leaving them empty publishes an
    entry that answers "is this poisonous?" with silence — see the warning
    returned by the approval endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    botanical_name: str = Field(..., min_length=1, max_length=255)
    family_name: str | None = None
    common_names: list[str] | None = None
    origin_status: str | None = None
    distribution_formally_listed: list[str] | None = None
    distribution_observed: list[str] | None = None
    habitat: str | None = None
    seasonality_of_toxicity: str | None = None
    toxic_parts: list[str] | None = None
    toxic_to_humans: bool | None = None
    toxic_to_animals: list[str] | None = None
    lethality: str | None = None
    toxin_profile: dict[str, Any] | None = None
    health_effects: dict[str, Any] | None = None
    morphological_attributes: dict[str, Any] | None = None
    historical_case_records: str | None = None


class SubmissionUpdate(BaseModel):
    """Admin action: approve or reject a submission.

    Approving requires `plant`: the reviewer's identification, which becomes a
    new row in `plants` with the submission's photos copied across.
    """

    status: ReviewAction
    reviewer_notes: str | None = None
    plant: PlantIdentification | None = None

    @model_validator(mode="after")
    def _identification_required_to_approve(self) -> "SubmissionUpdate":
        """Approval without an identification would create nothing."""
        if self.status == "approved" and self.plant is None:
            raise ValueError(
                "Approving a submission requires 'plant' with at least a botanical_name."
            )
        if self.status == "rejected" and self.plant is not None:
            raise ValueError("'plant' is only meaningful when approving.")
        return self
