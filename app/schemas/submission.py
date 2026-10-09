"""Submission request/response schemas — match real Supabase schema."""

import hashlib
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from app.models.plant import TOXICITY_SPECIES

SubmissionStatus = Literal["pending", "approved", "rejected"]
ReviewAction = Literal["approved", "rejected"]

#: Longest `reviewer_notes` a review accepts — a few paragraphs.
REVIEWER_NOTES_MAX_LENGTH = 2000

#: The only status in which a submission may still change — a contributor may
#: edit it, and it is the only state a review decision can be made from.
PENDING_STATUS = "pending"

#: Upper bound on images per submission, enforced on create and when adding.
MAX_PHOTOS_PER_SUBMISSION = 10


def content_fingerprint(
    *,
    contributor_name: str,
    user_description: str | None,
    location_state: str | None,
    latitude: Decimal | float | None,
    longitude: Decimal | float | None,
    photos: Iterable[tuple[UUID | str, bool]],
) -> str:
    """A short digest of everything a reviewer looks at before deciding.

    Optimistic concurrency without a schema change. `submissions` has no
    version column and the database is not managed by Alembic — no migration
    history, no `alembic_version` table — so adding one means hand-editing
    production. This asks the question directly instead: has any of what the
    reviewer saw changed since they loaded it?

    Photos are included because they are the part that matters most. A
    contributor swapping one while the reviewer reads would otherwise publish
    a photograph nobody approved.

    Deliberately excluded: `status`, `reviewer_notes`, `reviewed_by`,
    `reviewed_at`. Those belong to the reviewer, and folding them in would
    make a reviewer's own write invalidate the version they are holding.
    """
    parts = [
        contributor_name or "",
        user_description or "",
        location_state or "",
        _number(latitude),
        _number(longitude),
    ]
    # Sorted: the order rows come back in is not part of what changed.
    parts += [f"{pid}:{int(primary)}" for pid, primary in sorted(
        ((str(pid), bool(primary)) for pid, primary in photos)
    )]
    joined = "\x1f".join(parts).encode()
    return hashlib.blake2b(joined, digest_size=8).hexdigest()


def _number(value: Decimal | float | None) -> str:
    """Normalise a coordinate so 1.50 and 1.5 do not read as a change."""
    if value is None:
        return ""
    return format(Decimal(str(value)).normalize(), "f")


class SubmissionImageOut(BaseModel):
    """One image row joined into a submission."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    image_url: str
    is_primary: bool = False


#: Valid coordinate ranges. Nothing bounded them, so `latitude=999` was stored
#: as given and a map placing the observation would fail or wrap silently.
Latitude = Annotated[Decimal, Field(ge=-90, le=90)]
Longitude = Annotated[Decimal, Field(ge=-180, le=180)]


class SubmissionCreate(BaseModel):
    """Non-file fields accepted by `POST /submissions/`.

    Whitespace is stripped before the length checks, so a name of `"   "`
    fails `min_length` instead of being stored as an empty string.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    user_description: str | None = None
    contributor_name: str = Field(..., min_length=1, max_length=255)
    contributor_email: EmailStr
    location_state: str | None = None
    latitude: Latitude | None = None
    longitude: Longitude | None = None


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def content_version(self) -> str:
        """What a reviewer must send back to prove they saw this version.

        Derived rather than stored, so it cannot drift from the content it
        describes and needs no column of its own.
        """
        return content_fingerprint(
            contributor_name=self.contributor_name,
            user_description=self.user_description,
            location_state=self.location_state,
            latitude=self.latitude,
            longitude=self.longitude,
            photos=[(image.id, image.is_primary) for image in self.images],
        )


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


#: Config shared by everything a reviewer types. Whitespace is stripped first
#: so that `"   "` is as empty as `""`, and both are then cleared by
#: `_clear_blanks` — otherwise a blank answer is stored as a blank string, not
#: as the `'Unknown'` the dataset uses, and `botanical_name="   "` passes
#: `min_length=1` and publishes a plant with no name.
_REVIEWER_INPUT = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _clear_blanks(model: BaseModel) -> None:
    """Turn every blank answer on `model` into "not answered".

    An empty string becomes None, blank entries are dropped from a list, and a
    list left with nothing in it becomes None. `_create_plant` dumps with
    `exclude_none`, so all three then fall through to the `'Unknown'` default
    rather than being written as `""`, `[""]` or `[]`.
    """
    # Extra keys too: `MorphologicalAttributes` accepts any, and a blank one
    # was otherwise stored as "" rather than left out.
    for name in [*type(model).model_fields, *(model.model_extra or {})]:
        value = getattr(model, name)
        if isinstance(value, str) and not value.strip():
            setattr(model, name, None)
        elif isinstance(value, list):
            kept = [v for v in value if not (isinstance(v, str) and not v.strip())]
            setattr(model, name, kept or None)


#: The two values `toxicity_by_species` holds across all 1,000 species entries
#: in the curated data — 44 `'Toxic'`, 956 `'Unknown'`, nothing else.
SpeciesFinding = Literal["Toxic", "Unknown"]


class ToxinProfile(BaseModel):
    """What the poison is, chemically.

    The keys are `plants_v2`'s, and they are not v1's. v1 recorded
    `{toxins, chemical_class, onset}`; v2 splits the chemistry into a primary
    and a secondary class and names the compounds separately, and has nowhere
    to put onset. A reviewer who needs to record how fast symptoms appear puts
    it in `health_effects`, which is prose in v2.
    """

    model_config = _REVIEWER_INPUT

    #: Chemical family, e.g. "Lectins / Toxalbumins".
    primary_class: str | None = None
    #: A second family where the plant carries more than one, e.g. "Alkaloids".
    secondary_class: str | None = None
    #: The compounds themselves, e.g. "Abrin- plant lectin, toxalbumin".
    toxins_implicated: str | None = None

    @model_validator(mode="after")
    def _blanks_are_unanswered(self) -> "ToxinProfile":
        _clear_blanks(self)
        return self


class ToxicityBySpecies(BaseModel):
    """Which species this plant is toxic to.

    Replaces v1's `toxic_to_humans` boolean plus `toxic_to_animals` list,
    which could not express "toxic to horses, not yet established for dogs" —
    the commonest state of this data by a wide margin.

    Each species is `"Toxic"` where toxicity is established and `"Unknown"`
    otherwise — the only two values the dataset uses. `extra="forbid"` because
    a typo'd species key would silently record a finding under a name nothing
    reads.

    The values are closed for the same reason. The derived `toxic_to_humans`
    and the species search both test for `"Toxic"` exactly, so a reviewer who
    typed `"toxic"`, `"Yes"` or `"Mildly toxic"` would have recorded a finding
    that the API then reports as *not* toxic. Case is forgiven, since it can
    only have meant one thing; anything else is a 422 naming the two values.
    Qualifications belong in `notes`.
    """

    model_config = _REVIEWER_INPUT

    humans: SpeciesFinding | None = None
    cattle: SpeciesFinding | None = None
    horses: SpeciesFinding | None = None
    sheep: SpeciesFinding | None = None
    goats: SpeciesFinding | None = None
    pigs: SpeciesFinding | None = None
    poultry: SpeciesFinding | None = None
    dogs: SpeciesFinding | None = None
    cats: SpeciesFinding | None = None
    #: Any species the named keys do not cover.
    other: SpeciesFinding | None = None
    #: Free text qualifying the above, e.g. which part or what dose.
    notes: str | None = None

    @field_validator(*TOXICITY_SPECIES, mode="before")
    @classmethod
    def _normalise_finding(cls, value: object) -> object:
        """`" toxic "` → `"Toxic"`; blank → unanswered; anything else as given."""
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if not cleaned:
            return None
        for finding in ("Toxic", "Unknown"):
            if cleaned.casefold() == finding.casefold():
                return finding
        return cleaned

    @model_validator(mode="after")
    def _blanks_are_unanswered(self) -> "ToxicityBySpecies":
        _clear_blanks(self)
        return self


class MorphologicalAttributes(BaseModel):
    """How to recognise the plant.

    v2's keys, which are more measurable than v1's prose descriptions of leaf
    and flower: a height in metres, a growth form, a fruit colour, the months
    it flowers. Left open — describing a plant is not a closed set, and a
    reviewer who needs to record a bulb or a rhizome should not wait for a
    schema change. Extra keys are kept as given.
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    #: Mature height in metres. A number in the curated data, e.g. 1.5.
    height_m: float | str | None = None
    #: e.g. "herb subshrub", "climber_herbaceous", "tree".
    growth_form: str | None = None
    #: e.g. "woody", "herbaceous".
    woodiness: str | None = None
    #: e.g. "annual", "perennial".
    life_history: str | None = None
    #: e.g. "black", "brown".
    fruit_colour: str | None = None
    #: Three-letter month abbreviations, e.g. ["Feb", "Mar", "Apr"].
    flowering_time: list[str] | None = None

    @model_validator(mode="after")
    def _blanks_are_unanswered(self) -> "MorphologicalAttributes":
        _clear_blanks(self)
        return self


class PlantIdentification(BaseModel):
    """The botanical record a reviewer supplies when approving a submission.

    A submission carries a photo, a description and a location — none of the
    fields `plants_v2` needs. `botanical_name` has no counterpart in
    `submissions`, so approval cannot be a mechanical copy: the identification
    and the toxicity profile come from the reviewer.

    Only `botanical_name` is required. Every other column in `plants_v2` is
    NOT NULL, but the dataset's convention for a gap is the string
    `"Unknown"`, so `SubmissionService._create_plant` fills that in for
    whatever is left blank rather than refusing the approval. Leaving the
    toxicity fields empty publishes an entry that answers "is this poisonous?"
    with `"Unknown"` — see the warning returned by the approval endpoint.
    """

    model_config = _REVIEWER_INPUT

    botanical_name: str = Field(..., min_length=1, max_length=255)
    family_name: str | None = None
    common_names: list[str] | None = None
    origin_status: str | None = None
    #: v1's `distribution_formally_listed` and `distribution_observed` merged.
    distribution: list[str] | None = None
    habitat: str | None = None
    seasonality_of_toxicity: str | None = None
    toxic_parts: list[str] | None = None
    toxicity_by_species: ToxicityBySpecies | None = None
    lethality: str | None = None
    toxin_profile: ToxinProfile | None = None
    #: Prose in v2, not the structured object v1 used.
    health_effects: str | None = None
    morphological_attributes: MorphologicalAttributes | None = None

    @model_validator(mode="after")
    def _drop_empty_structures(self) -> "PlantIdentification":
        """Treat a nested object with nothing in it as absent.

        `{"toxin_profile": {}}` and omitting the field mean the same thing to
        a reviewer, but storing `{}` would make the field truthy and silence
        the warning that says nothing is recorded — the entry would look
        answered while saying nothing.

        Blank strings and lists are cleared first, for the same reason one
        level up: `family_name=""` should become `'Unknown'`, not `""`.
        """
        _clear_blanks(self)
        for name in ("toxin_profile", "toxicity_by_species", "morphological_attributes"):
            value = getattr(self, name)
            if value is not None and not value.model_dump(exclude_none=True):
                setattr(self, name, None)
        return self


class SubmissionUpdate(BaseModel):
    """Admin action: approve or reject a submission.

    Approving needs exactly one of:

    * `plant` — the reviewer's identification of a species not yet in the
      library, which becomes a new row in `plants_v2`;
    * `plant_id` — an existing plant this is another observation of, whose
      gallery the photos join.

    Either way the submission's photos are copied across.
    """

    model_config = ConfigDict(extra="forbid")

    status: ReviewAction
    #: Quoted in full in the contributor's email, unlike their own description
    #: (which the email shortens), so it is bounded here instead.
    reviewer_notes: str | None = Field(None, max_length=REVIEWER_NOTES_MAX_LENGTH)
    plant: PlantIdentification | None = None
    #: Most observations are of a species the library already holds; without
    #: this, approving one published a second entry under the same name.
    plant_id: int | None = Field(
        None, ge=1, description="Add the photos to this existing plant instead of creating one."
    )
    #: The `content_version` the reviewer was looking at. Required, not
    #: optional: a guard a client can forget to send is not a guard, and
    #: approving publishes photographs — the one thing that must not turn out
    #: to be different from what was reviewed.
    expected_version: str = Field(
        ...,
        min_length=1,
        description="The submission's content_version as returned by GET; a mismatch is 409.",
    )

    @model_validator(mode="after")
    def _identification_required_to_approve(self) -> "SubmissionUpdate":
        """Approval needs a plant to publish to: a new one, or an existing one."""
        if self.status == "approved":
            if self.plant is None and self.plant_id is None:
                raise ValueError(
                    "Approving a submission requires 'plant' with at least a "
                    "botanical_name, or 'plant_id' to add it to an existing plant."
                )
            if self.plant is not None and self.plant_id is not None:
                raise ValueError(
                    "Send 'plant' to create a new entry or 'plant_id' to add to an "
                    "existing one, not both."
                )
        if self.status == "rejected" and (self.plant is not None or self.plant_id is not None):
            raise ValueError("'plant' and 'plant_id' are only meaningful when approving.")
        return self


class SubmissionContentUpdate(BaseModel):
    """Contributor edit of their own still-pending submission.

    Every field is optional and applied only when present in the request
    body, so a client can send just the field it changed without blanking
    the rest. `contributor_email` and `status` are deliberately absent:
    ownership is derived from the email, and the status belongs to the
    reviewer.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    contributor_name: str | None = Field(None, min_length=1, max_length=255)
    user_description: str | None = None
    location_state: str | None = None
    latitude: Latitude | None = None
    longitude: Longitude | None = None

    @field_validator("contributor_name")
    @classmethod
    def _name_cannot_be_cleared(cls, value: str | None) -> str | None:
        """Omit the name to keep it; `null` is not a name.

        The column is NOT NULL, so `{"contributor_name": null}` used to reach
        the UPDATE and come back as a 500. Unlike the other fields, there is
        nothing for null to mean here.
        """
        if value is None:
            raise ValueError("cannot be null; omit it to leave the name unchanged")
        return value

    @field_validator("user_description", "location_state")
    @classmethod
    def _blank_clears(cls, value: str | None) -> str | None:
        """A blank answer clears the field, the same as `null` does.

        Matches `POST /submissions/`, which stores an empty description as
        NULL rather than as `""`.
        """
        return value or None
