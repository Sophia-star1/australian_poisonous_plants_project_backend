"""Submission request/response schemas — match real Supabase schema."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

SubmissionStatus = Literal["pending", "approved", "rejected"]
ReviewAction = Literal["approved", "rejected"]


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


class SubmissionFilter(BaseModel):
    """Query-string filters for the admin submissions list."""

    status: SubmissionStatus | None = None
    contributor_name: str | None = None
    contributor_email: str | None = None
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)


class SubmissionUpdate(BaseModel):
    """Admin action: approve or reject a submission."""

    status: ReviewAction
    reviewer_notes: str | None = None
