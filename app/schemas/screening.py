"""Schemas for the admin screening-threshold endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ScreeningConfigOut(BaseModel):
    """The thresholds the classifier grades against.

    `source` is `"database"` once an admin has set the pair and `"defaults"`
    before that, when the configured values (`PLANT_REJECT_BELOW` /
    `PLANT_ACCEPT_AT`) govern.
    """

    reject_below: float = Field(description="Photos scoring below this are refused.")
    accept_at: float = Field(
        description="At or above this a photo passes cleanly; between the two it is "
        "accepted but flagged for the reviewer."
    )
    source: str
    updated_at: datetime | None = None
    updated_by: str | None = None


class ScreeningConfigUpdate(BaseModel):
    """One or both thresholds. The pair must stay ordered when merged.

    Range and ordering are checked at the endpoint against the stored values,
    because one field alone cannot be judged without the other.
    """

    model_config = ConfigDict(extra="forbid")

    reject_below: float | None = Field(None, ge=0, le=1)
    accept_at: float | None = Field(None, ge=0, le=1)
