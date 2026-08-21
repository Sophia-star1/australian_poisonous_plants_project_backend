"""Tests for screening uploads through the plant classifier.

The classifier itself is covered in `test_plant_classifier.py`. What matters
here is how the upload path uses it:

* a refusal must cost nothing in storage;
* a classifier that cannot load must not take uploads down with it;
* reading the bytes to score them must not consume them before the upload.

That last one is the subtle failure: screening reads each photo, so without
rewinding the handle the upload that follows sees an empty file and stores
zero bytes — with no error anywhere, because every layer did what it was told.
"""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from PIL import Image

from app.config import settings
from app.db.session import get_db
from app.main import app
from app.ml.classifier import Decision, UndecodableImageError, Verdict, reset_classifier
from app.services import submission_service as svc
from app.services.storage_service import StoredFile


class FakeSession:
    """Enough AsyncSession for `create()`."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if getattr(obj, "submitted_at", None) is None:
                obj.submitted_at = datetime.now(UTC)
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
def _isolated() -> AsyncIterator[FakeSession]:
    session = FakeSession()

    async def _fake_db() -> AsyncIterator[FakeSession]:
        yield session

    app.dependency_overrides[get_db] = _fake_db
    reset_classifier()
    yield session
    app.dependency_overrides.pop(get_db, None)
    reset_classifier()


@pytest.fixture
def uploads(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record uploads, but let the real read path run so rewinding is tested."""
    seen: list[dict[str, Any]] = []

    async def _upload_bytes(
        self: Any, data: bytes, key: str, bucket: str, content_type: str
    ) -> StoredFile:
        seen.append({"key": key, "bucket": bucket, "size": len(data), "data": data})
        return StoredFile(key=key, url=f"https://proj.supabase.co/{bucket}/{key}")

    monkeypatch.setattr(
        "app.services.submission_service.StorageService.upload_bytes", _upload_bytes
    )
    return seen


@pytest.fixture
def verdicts(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Force the classifier's answers; records the bytes it was handed."""

    def _apply(*decisions: tuple[Decision, float]) -> list[bytes]:
        seen: list[bytes] = []
        queue = list(decisions)

        class _Stub:
            def classify(self, data: bytes) -> Verdict:
                seen.append(data)
                decision, confidence = queue.pop(0) if queue else decisions[-1]
                return Verdict(decision=decision, confidence=confidence)

        monkeypatch.setattr(svc, "get_classifier", lambda: _Stub())
        return seen

    return _apply


def _jpeg(colour: tuple[int, int, int] = (30, 140, 60), size: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), colour).save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


def _form() -> dict[str, str]:
    return {"contributor_name": "Ada", "contributor_email": "ada@example.com"}


def _file(name: str = "a.jpg", data: bytes | None = None) -> tuple[str, bytes, str]:
    return (name, _jpeg() if data is None else data, "image/jpeg")


async def _post(client: AsyncClient, *files: tuple[str, bytes, str]) -> Any:
    payload = [("photos", f) for f in files] or [("photos", _file())]
    return await client.post("/api/v1/submissions/", data=_form(), files=payload)


# ── the bytes survive being read twice ───────────────────────────────────────


@pytest.mark.asyncio
async def test_screening_does_not_consume_the_upload(
    async_client: AsyncClient, uploads: list[dict[str, Any]], verdicts: Any
) -> None:
    """Regression: without a rewind the stored object would be empty.

    Screening reads the whole file to score it. If the handle is not returned
    to the start, the upload that follows reads nothing and stores a zero-byte
    object — silently, since no layer treats an empty read as an error.
    """
    verdicts((Decision.ACCEPT, 0.99))
    body = _jpeg()

    response = await _post(async_client, _file(data=body))

    assert response.status_code == 201
    assert len(uploads) == 1
    assert uploads[0]["size"] == len(body), "the upload path got a truncated file"
    assert uploads[0]["data"] == body


@pytest.mark.asyncio
async def test_every_photo_is_scored(
    async_client: AsyncClient, uploads: list[dict[str, Any]], verdicts: Any
) -> None:
    """Three photos, three verdicts, three intact uploads."""
    seen = verdicts((Decision.ACCEPT, 0.99), (Decision.FLAG, 0.4), (Decision.ACCEPT, 0.9))
    bodies = [_jpeg((i * 60, 120, 40)) for i in range(3)]

    response = await _post(async_client, *(_file(f"{i}.jpg", b) for i, b in enumerate(bodies)))

    assert response.status_code == 201
    assert len(seen) == 3
    assert [u["data"] for u in uploads] == bodies


# ── refusal ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_submission_of_non_plants_is_refused(
    async_client: AsyncClient, uploads: list[dict[str, Any]], verdicts: Any
) -> None:
    """The feature's whole purpose."""
    verdicts((Decision.REJECT, 0.02))

    response = await _post(async_client)

    assert response.status_code == 422
    assert "appear to show a plant" in response.json()["message"]


@pytest.mark.asyncio
async def test_a_refusal_costs_no_storage(
    async_client: AsyncClient, uploads: list[dict[str, Any]], verdicts: Any
) -> None:
    """Screening runs before the first byte leaves the request body.

    Uploading and then deleting would work too, but it would pay for the
    bandwidth of exactly the traffic this exists to turn away.
    """
    verdicts((Decision.REJECT, 0.01), (Decision.REJECT, 0.03))

    response = await _post(async_client, _file("a.jpg"), _file("b.jpg"))

    assert response.status_code == 422
    assert uploads == [], "a refused submission must not touch the bucket"


@pytest.mark.asyncio
async def test_the_message_tells_the_contributor_what_to_do(
    async_client: AsyncClient, uploads: list[dict[str, Any]], verdicts: Any
) -> None:
    """A rejection nobody can act on is just a failure."""
    verdicts((Decision.REJECT, 0.02))
    message = (await _post(async_client)).json()["message"]
    assert "leaf" in message and "upload a photo of the plant" in message.lower()


@pytest.mark.asyncio
async def test_one_bad_photo_among_several_is_not_a_refusal(
    async_client: AsyncClient, uploads: list[dict[str, Any]], verdicts: Any
) -> None:
    """Only a submission with nothing plant-like in it is turned away.

    A contributor photographing a shrub from four angles may well catch one
    frame of dirt. Refusing the lot would lose the other three, and the model
    is not accurate enough to be given that authority — it puts 7 of this
    project's own 104 curated plant photographs below its default cutoff.
    """
    verdicts((Decision.REJECT, 0.01), (Decision.ACCEPT, 0.98))

    response = await _post(async_client, _file("bad.jpg"), _file("good.jpg"))

    assert response.status_code == 201
    assert len(uploads) == 2, "both photos are kept"


@pytest.mark.asyncio
async def test_an_uncertain_photo_is_accepted_not_refused(
    async_client: AsyncClient, uploads: list[dict[str, Any]], verdicts: Any
) -> None:
    """The middle band exists for specimen shots and bark close-ups."""
    verdicts((Decision.FLAG, 0.35))

    response = await _post(async_client)

    assert response.status_code == 201
    assert response.json()["data"]["ai_is_plant"] is False, "recorded, not rejected"


# ── what gets recorded ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_verdict_is_stored_on_the_submission(
    async_client: AsyncClient, uploads: list[dict[str, Any]], verdicts: Any, _isolated: Any
) -> None:
    """A reviewer needs the score, not just the boolean."""
    verdicts((Decision.ACCEPT, 0.97), (Decision.FLAG, 0.42))

    await _post(async_client, _file("a.jpg"), _file("b.jpg"))

    row = _isolated.added[0]
    assert row.ai_is_plant is True
    record = row.ai_top_matches["screening"]
    assert record["plant_probability"] == 0.97, "the best photo speaks for the submission"
    assert [p["plant_probability"] for p in record["photos"]] == [0.97, 0.42]
    # ai_top_matches is jsonb; anything not JSON-native fails at the driver.
    assert json.loads(json.dumps(row.ai_top_matches)) == row.ai_top_matches


@pytest.mark.asyncio
async def test_unscreened_is_null_not_false(
    async_client: AsyncClient, uploads: list[dict[str, Any]], monkeypatch: Any, _isolated: Any
) -> None:
    """`false` means the model doubted it; `NULL` means nobody looked.

    Collapsing the two would show a reviewer a warning that was never earned.
    """
    monkeypatch.setattr(svc, "get_classifier", lambda: None)

    response = await _post(async_client)

    assert response.status_code == 201
    assert _isolated.added[0].ai_is_plant is None
    assert _isolated.added[0].ai_top_matches is None


# ── degrading ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uploads_still_work_without_a_classifier(
    async_client: AsyncClient, uploads: list[dict[str, Any]], monkeypatch: Any
) -> None:
    """Missing weights must not become an outage."""
    monkeypatch.setattr(settings, "PLANT_CLASSIFIER_ENABLED", False)

    response = await _post(async_client)

    assert response.status_code == 201
    assert len(uploads) == 1


@pytest.mark.asyncio
async def test_a_file_that_is_not_really_an_image_is_refused(
    async_client: AsyncClient, uploads: list[dict[str, Any]], monkeypatch: Any
) -> None:
    """The declared content type says image; the bytes disagree.

    Nothing checked this before — screening is the first thing that tries to
    decode an upload. A reviewer cannot look at it either way.
    """

    class _Stub:
        def classify(self, data: bytes) -> Verdict:
            raise UndecodableImageError("nope")

    monkeypatch.setattr(svc, "get_classifier", lambda: _Stub())

    response = await _post(async_client, ("x.jpg", b"not an image", "image/jpeg"))

    assert response.status_code == 422
    assert "not a readable image" in response.json()["message"]
    assert uploads == []


@pytest.mark.asyncio
async def test_oversized_photos_are_caught_during_screening(
    async_client: AsyncClient, uploads: list[dict[str, Any]], verdicts: Any, monkeypatch: Any
) -> None:
    """The size guard moved earlier along with the read; it still fires."""
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 100)
    verdicts((Decision.ACCEPT, 0.99))

    response = await _post(async_client, _file(data=_jpeg(size=256)))

    assert response.status_code == 422
    assert "upload limit" in response.json()["message"]
    assert uploads == []
