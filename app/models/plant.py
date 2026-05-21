"""Plant and PlantImage ORM models — matches Supabase `public.plants`."""

from typing import Any

from sqlalchemy import ARRAY, BigInteger, Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, CreatedAtMixin


class Plant(Base, CreatedAtMixin):
    """A poisonous plant entry."""

    __tablename__ = "plants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    botanical_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    family_name: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    common_names: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)

    origin_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    distribution_formally_listed: Mapped[list[str] | None] = mapped_column(
        ARRAY(String), nullable=True
    )
    distribution_observed: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    habitat: Mapped[str | None] = mapped_column(Text, nullable=True)
    seasonality_of_toxicity: Mapped[str | None] = mapped_column(Text, nullable=True)

    toxic_parts: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    toxic_to_humans: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    toxic_to_animals: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    lethality: Mapped[str | None] = mapped_column(Text, nullable=True)

    toxin_profile: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    health_effects: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    morphological_attributes: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    photographs: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    historical_case_records: Mapped[str | None] = mapped_column(Text, nullable=True)

    images: Mapped[list["PlantImage"]] = relationship(
        "PlantImage",
        back_populates="plant",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class PlantImage(Base, CreatedAtMixin):
    """A photo attached to a plant entry."""

    __tablename__ = "plant_images"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    plant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("plants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    image_url: Mapped[str] = mapped_column(Text, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    plant: Mapped[Plant] = relationship("Plant", back_populates="images")
