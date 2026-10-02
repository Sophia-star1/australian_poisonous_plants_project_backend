"""Submission ORM models — match Supabase `public.submissions` + `submission_images`."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Submission(Base):
    """A user-uploaded plant observation pending expert review."""

    __tablename__ = "submissions"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    contributor_name: Mapped[str] = mapped_column(Text, nullable=False)
    contributor_email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    submitted_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    user_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    ai_is_plant: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ai_top_matches: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    images: Mapped[list["SubmissionImage"]] = relationship(
        "SubmissionImage",
        back_populates="submission",
        cascade="all, delete-orphan",
        lazy="selectin",
        # Deterministic order, so a reviewer looking at the same submission
        # twice sees the photos in the same sequence and the lead photo stays
        # first. `selectinload` emits no ORDER BY of its own, which leaves the
        # sequence unspecified.
        #
        # Note what this does *not* give: upload order. `id` here is a UUID —
        # random, not monotonic — and `submission_images` has no `created_at`
        # column, so the order the contributor picked their photos in is simply
        # not recorded anywhere. Adding a `created_at` or `sort_order` column is
        # the only way to recover it; until then "second photo" is not a thing
        # this API can express.
        order_by="(desc(SubmissionImage.is_primary), SubmissionImage.id)",
    )


class SubmissionImage(Base):
    """One photo attached to a submission."""

    __tablename__ = "submission_images"

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    submission_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    image_url: Mapped[str] = mapped_column(Text, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    submission: Mapped[Submission] = relationship("Submission", back_populates="images")
