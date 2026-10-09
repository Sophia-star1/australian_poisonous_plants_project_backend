"""Screening-threshold configuration row.

A single-row table (`id = 1`) written by the admin endpoints in
`app/api/v1/admin.py`. Why the thresholds live here rather than in an
environment variable is explained in `app/services/screening_config_service.py`.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, SmallInteger, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ScreeningThresholds(Base):
    """The AI screening acceptance/refusal bands, adjustable at runtime."""

    __tablename__ = "screening_thresholds"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    reject_below: Mapped[float] = mapped_column(Float, nullable=False)
    accept_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(Text, nullable=False)
