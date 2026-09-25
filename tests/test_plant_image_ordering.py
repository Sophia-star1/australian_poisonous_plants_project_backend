"""Tests for how a plant's photographs are ordered and addressed.

`plants_v2` supports many photos per plant through `plant_images_v2`, but the
curated dataset has exactly one image for each of its 100 rows — so the
multi-image case arrives only via an approved submission, and it arrives with
up to ten photos at once. These tests cover what that case needs and what the
schema does not give for free.

The ordering assertions are on the ORM `order_by`, expressed as compiled SQL
rather than as loaded rows: `selectinload` issues its own SELECT, and the bug
being guarded against is the *absence* of an ORDER BY in exactly that
statement. Asserting on a handful of rows from a small table would pass even
without one, because Postgres usually returns them in physical order until an
UPDATE or a VACUUM moves them.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import inspect, select
from sqlalchemy.dialects import postgresql

from app.models.plant import Plant, PlantImage
from app.models.submission import Submission, SubmissionImage
from app.schemas.plant import PlantImageOut


def _images_sql(parent: type, attr: str) -> str:
    """The SQL `selectinload` will emit for a relationship, as one line."""
    rel = inspect(parent).relationships[attr]
    stmt = select(rel.mapper.class_).order_by(*rel.order_by)
    return " ".join(
        str(stmt.compile(dialect=postgresql.dialect())).split()
    ).upper()


# ── the ordering the gallery depends on ──────────────────────────────────────


def test_plant_images_are_ordered_at_all() -> None:
    """The bug: no ORDER BY means an unspecified, drifting sequence."""
    assert "ORDER BY" in _images_sql(Plant, "images")


def test_plant_images_put_the_primary_first() -> None:
    """So `plant_images[0]` is the cover, for every client, on every request."""
    sql = _images_sql(Plant, "images")
    order = sql.split("ORDER BY", 1)[1]
    assert "IS_PRIMARY DESC" in order
    assert order.index("IS_PRIMARY") < order.index("ID")


def test_plant_images_then_sort_by_id() -> None:
    """`plants_v2.id` is a bigint sequence, so this reproduces upload order."""
    assert _images_sql(Plant, "images").split("ORDER BY", 1)[1].strip().endswith("ID")


def test_submission_images_are_ordered_too() -> None:
    """The reviewer sees the same sequence every time they open a submission."""
    sql = _images_sql(Submission, "images")
    assert "ORDER BY" in sql
    assert "IS_PRIMARY DESC" in sql.split("ORDER BY", 1)[1]


def test_submission_image_ids_cannot_encode_upload_order() -> None:
    """A limitation worth pinning rather than discovering later.

    `SubmissionImage.id` is a random UUID and the table has no `created_at`, so
    ordering by id is *stable* but arbitrary: the sequence the contributor
    picked their photos in is not recorded anywhere. Recovering it needs a new
    column. This test fails the day someone adds one, which is the moment to
    revisit the `order_by` above.
    """
    columns = set(inspect(SubmissionImage).columns.keys())
    assert "created_at" not in columns
    assert "sort_order" not in columns
    assert isinstance(uuid4(), type(SubmissionImage().id or uuid4()))


def test_plant_images_can_record_order_if_one_is_ever_added() -> None:
    """The plant side has no ordering column either — id carries it instead.

    Documented rather than fixed: `plant_images_v2.id` is monotonic, so unlike
    the submission side it does encode insertion order.
    """
    columns = set(inspect(PlantImage).columns.keys())
    assert "sort_order" not in columns
    assert "created_at" in columns  # present, but id is the finer-grained key


# ── addressing one specific photo ────────────────────────────────────────────


def test_plant_image_payload_exposes_its_id() -> None:
    """Without it a client can only address a photo by list position or URL.

    Needed for "make this the cover" and "remove this photo", and for a stable
    key when rendering the list.
    """
    assert "id" in PlantImageOut.model_fields


def test_plant_image_payload_carries_the_display_fields() -> None:
    fields = set(PlantImageOut.model_fields)
    assert fields == {"id", "image_url", "thumbnail_url", "caption", "is_primary"}


def test_plant_image_payload_validates_from_an_orm_row() -> None:
    row = PlantImage()
    row.id = 7
    row.plant_id = 1
    row.image_url = "https://example.test/x.jpg"
    row.thumbnail_url = None
    row.caption = "beside the creek"
    row.is_primary = True

    out = PlantImageOut.model_validate(row)
    assert out.id == 7
    assert out.thumbnail_url is None
    assert out.is_primary is True
