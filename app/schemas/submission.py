"""Submission request/response schemas — match real Supabase schema."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

SubmissionStatus = Literal["pending", "approved", "rejected"]
ReviewAction = Literal["approved", "rejected"]

#: The only status in which a submission may still change — a contributor may
#: edit it, and it is the only state a review decision can be made from.
PENDING_STATUS = "pending"

#: Upper bound on images per submission, enforced on create and when adding.
MAX_PHOTOS_PER_SUBMISSION = 10


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


class ToxinProfile(BaseModel):
    """What the poison is and how fast it acts.

    The keys come from the ten entries already in `plants`, every one of which
    carries all three. They were free-form `dict` until now, which meant two
    reviewers could describe the same thing under different names and no
    client could rely on finding either.
    """

    model_config = ConfigDict(extra="forbid")

    #: The toxic compound or compounds, e.g. "Abrin".
    toxins: str | None = None
    #: Chemical family, e.g. "toxalbumin / plant lectin".
    chemical_class: str | None = None
    #: How long before symptoms appear, e.g. "delayed 6-24 hours".
    onset: str | None = None


class HealthEffects(BaseModel):
    """What exposure does to a person or animal.

    This is the field triage actually reads, so `symptoms` is a list rather
    than prose: it is what a symptom search has to match against.
    """

    model_config = ConfigDict(extra="forbid")

    #: Individual symptoms, e.g. ["vomiting", "diarrhoea", "abdominal pain"].
    symptoms: list[str] | None = None
    #: The clinical pattern, e.g. "gastrointestinal and systemic toxicity".
    toxidrome: str | None = None
    #: Systems affected, e.g. ["gastrointestinal", "hepatic"].
    target_organs: list[str] | None = None


class MorphologicalAttributes(BaseModel):
    """How to recognise the plant.

    Unlike the other two this one stays open. The named keys are what the
    existing entries use — `growth_habit`, `life_history` and `flower` appear
    in all ten, the rest in some — but describing a plant is not a closed set,
    and a reviewer who needs to record a bulb or a rhizome should not have to
    wait for a schema change. Extra keys are kept as given.
    """

    model_config = ConfigDict(extra="allow")

    #: Present in every existing entry.
    growth_habit: str | None = None
    life_history: str | None = None
    flower: str | None = None
    #: Present in some.
    leaf: str | None = None
    fruit: str | None = None
    sap: str | None = None
    stem: str | None = None
    odor: str | None = None
    armament: str | None = None


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
    toxin_profile: ToxinProfile | None = None
    health_effects: HealthEffects | None = None
    morphological_attributes: MorphologicalAttributes | None = None
    historical_case_records: str | None = None

    @model_validator(mode="after")
    def _drop_empty_structures(self) -> "PlantIdentification":
        """Treat a nested object with nothing in it as absent.

        `{"toxin_profile": {}}` and omitting the field mean the same thing to
        a reviewer, but storing `{}` would make `health_effects` truthy and
        silence the warning that says nothing is recorded — the entry would
        look answered while saying nothing.
        """
        for name in ("toxin_profile", "health_effects", "morphological_attributes"):
            value = getattr(self, name)
            if value is not None and not value.model_dump(exclude_none=True):
                setattr(self, name, None)
        return self


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


class SubmissionContentUpdate(BaseModel):
    """Contributor edit of their own still-pending submission.

    Every field is optional and applied only when present in the request
    body, so a client can send just the field it changed without blanking
    the rest. `contributor_email` and `status` are deliberately absent:
    ownership is derived from the email, and the status belongs to the
    reviewer.
    """

    model_config = ConfigDict(extra="forbid")

    contributor_name: str | None = Field(None, min_length=1, max_length=255)
    user_description: str | None = None
    location_state: str | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None
