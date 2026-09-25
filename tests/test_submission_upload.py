"""Tests for the submission upload path.

Covers what the endpoint accepts and what key each photo is stored under.
Storage is stubbed, so nothing leaves the process.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.api.v1 import submissions as api
from app.config import settings
from app.db.session import get_db
from app.main import app
from app.ml.classifier import reset_classifier
from app.services.storage_service import StorageError, StoredFile


class FakeSession:
    """Minimal stand-in for AsyncSession covering what `create()` uses.

    The submission row is assembled in Python before it is flushed, so the
    real database contributes nothing this test needs — except `submitted_at`,
    which comes from a server default. `flush` fills it in the same way
    Postgres would.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "submitted_at", None) is None:
                obj.submitted_at = datetime.now(UTC)
            # Image ids come from a Python-side default that only fires on a
            # real flush; assign them so the response model can validate.
            for image in getattr(obj, "images", []):
                if image.id is None:
                    image.id = uuid4()

    async def refresh(self, obj: Any, attribute_names: list[str] | None = None) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_database() -> AsyncIterator[None]:
    """Keep the suite off the real Supabase instance."""

    async def _fake_db() -> AsyncIterator[FakeSession]:
        yield FakeSession()

    app.dependency_overrides[get_db] = _fake_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _no_screening(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests are about storage keys, not about the classifier.

    The photo bodies here are a few bytes of nothing, which is not a decodable
    image; with screening on, every one of them would be refused before
    reaching the code under test. Screening has its own tests — see
    `test_submission_screening.py` — and turning it off here keeps each file
    testing one thing.
    """
    monkeypatch.setattr(settings, "PLANT_CLASSIFIER_ENABLED", False)
    reset_classifier()


@pytest.fixture
def uploads(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every upload the service performs instead of making one."""
    seen: list[dict[str, Any]] = []

    async def _upload_file(
        self: Any, file: Any, key: str, bucket: str | None = None
    ) -> StoredFile:
        seen.append({"key": key, "bucket": bucket, "filename": file.filename})
        return StoredFile(key=key, url=f"https://proj.supabase.co/{bucket}/{key}")

    monkeypatch.setattr(
        "app.services.submission_service.StorageService.upload_file", _upload_file
    )
    return seen


def _form() -> dict[str, str]:
    return {
        "contributor_name": "Ada",
        "contributor_email": "ada@example.com",
        "user_description": "found near the creek",
    }


def _photo(name: str = "IMG_0001.jpg", ctype: str = "image/jpeg") -> tuple[str, bytes, str]:
    return (name, b"fake-image-bytes", ctype)


@pytest.mark.asyncio
async def test_single_photo_is_stored_under_the_submission_id(
    async_client: AsyncClient, uploads: list[dict[str, Any]]
) -> None:
    """One photo, one upload, keyed by submission rather than filename."""
    response = await async_client.post(
        "/api/v1/submissions/", data=_form(), files={"photos": _photo()}
    )
    assert response.status_code == 201
    assert len(uploads) == 1

    key = uploads[0]["key"]
    assert uploads[0]["bucket"] == settings.SUPABASE_SUBMISSION_BUCKET
    assert key.startswith("submissions/")
    assert "IMG_0001" not in key, "the client filename must not become the key"

    submission_id = response.json()["data"]["id"]
    assert key.startswith(f"submissions/{submission_id}/")


@pytest.mark.asyncio
async def test_multiple_photos_each_get_a_distinct_key(
    async_client: AsyncClient, uploads: list[dict[str, Any]]
) -> None:
    """Regression: identical filenames used to produce identical keys."""
    files = [
        ("photos", _photo("IMG_0001.jpg")),
        ("photos", _photo("IMG_0001.jpg")),
        ("photos", _photo("IMG_0001.jpg")),
    ]
    response = await async_client.post("/api/v1/submissions/", data=_form(), files=files)

    assert response.status_code == 201
    assert len(uploads) == 3
    keys = [u["key"] for u in uploads]
    assert len(set(keys)) == 3, f"keys collided: {keys}"

    submission_id = response.json()["data"]["id"]
    assert all(k.startswith(f"submissions/{submission_id}/") for k in keys)


@pytest.mark.asyncio
async def test_first_photo_is_primary(
    async_client: AsyncClient, uploads: list[dict[str, Any]]
) -> None:
    """Exactly one image carries the primary flag, and it is the first."""
    files = [("photos", _photo("a.jpg")), ("photos", _photo("b.jpg"))]
    response = await async_client.post("/api/v1/submissions/", data=_form(), files=files)

    images = response.json()["data"]["images"]
    assert len(images) == 2
    assert sum(1 for i in images if i["is_primary"]) == 1


@pytest.mark.asyncio
async def test_stored_url_is_recorded_not_the_bare_key(
    async_client: AsyncClient, uploads: list[dict[str, Any]]
) -> None:
    """The row must hold a fetchable URL, as plant_images already does."""
    response = await async_client.post(
        "/api/v1/submissions/", data=_form(), files={"photos": _photo()}
    )
    url = response.json()["data"]["images"][0]["image_url"]
    assert url.startswith("https://"), f"expected a URL, got {url!r}"


@pytest.mark.asyncio
async def test_non_image_upload_is_rejected_before_storage(
    async_client: AsyncClient, uploads: list[dict[str, Any]]
) -> None:
    """Validation runs first, so a bad request costs no storage."""
    response = await async_client.post(
        "/api/v1/submissions/",
        data=_form(),
        files={"photos": ("payload.php", b"<?php ?>", "application/x-php")},
    )
    assert response.status_code == 422
    assert "unsupported type" in response.json()["message"]
    assert uploads == [], "nothing should have been uploaded"


@pytest.mark.asyncio
async def test_photo_count_is_capped(
    async_client: AsyncClient, uploads: list[dict[str, Any]]
) -> None:
    """A submission cannot carry an unbounded number of files."""
    files = [("photos", _photo(f"{i}.jpg")) for i in range(api.MAX_PHOTOS_PER_SUBMISSION + 1)]
    response = await async_client.post("/api/v1/submissions/", data=_form(), files=files)

    assert response.status_code == 422
    assert "at most" in response.json()["message"]
    assert uploads == []


@pytest.mark.asyncio
async def test_webp_and_png_are_accepted(
    async_client: AsyncClient, uploads: list[dict[str, Any]]
) -> None:
    """The whitelist is not JPEG-only."""
    files = [
        ("photos", _photo("a.png", "image/png")),
        ("photos", _photo("b.webp", "image/webp")),
    ]
    response = await async_client.post("/api/v1/submissions/", data=_form(), files=files)
    assert response.status_code == 201
    assert len(uploads) == 2


@pytest.mark.asyncio
async def test_content_type_with_parameters_is_accepted(
    async_client: AsyncClient, uploads: list[dict[str, Any]]
) -> None:
    """Some clients append a charset; the bare type is what matters."""
    response = await async_client.post(
        "/api/v1/submissions/",
        data=_form(),
        files={"photos": ("a.jpg", b"bytes", "image/jpeg; charset=binary")},
    )
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_storage_failure_does_not_create_a_submission(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row whose photos do not exist is worse than a failed request."""

    async def _boom(self: Any, *args: Any, **kwargs: Any) -> StoredFile:
        raise StorageError("Upload failed with HTTP 500.")

    monkeypatch.setattr(
        "app.services.submission_service.StorageService.upload_file", _boom
    )
    response = await async_client.post(
        "/api/v1/submissions/", data=_form(), files={"photos": _photo()}
    )
    assert response.status_code == 422
    assert "Upload failed" in response.json()["message"]


@pytest.mark.asyncio
async def test_submission_id_is_a_uuid_and_matches_the_photo_prefix(
    async_client: AsyncClient, uploads: list[dict[str, Any]]
) -> None:
    """The id is generated before upload so keys can be foldered by it."""
    response = await async_client.post(
        "/api/v1/submissions/", data=_form(), files={"photos": _photo()}
    )
    submission_id = response.json()["data"]["id"]
    UUID(submission_id)  # raises if malformed
    assert uploads[0]["key"].split("/")[1] == submission_id
