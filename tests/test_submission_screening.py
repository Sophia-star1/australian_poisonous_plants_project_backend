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
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import Update
from sqlalchemy.dialects import postgresql

from app.config import settings
from app.db.session import get_db
from app.main import app
from app.ml.classifier import (
    Decision,
    ImageTooLargeError,
    UndecodableImageError,
    Verdict,
    reset_classifier,
)
from app.services import submission_service as svc
from app.services.storage_service import StoredFile


def _upload(data: bytes, name: str = "a.jpg") -> Any:
    """A minimal UploadFile the screening path can read and rewind."""
    from starlette.datastructures import Headers, UploadFile

    return UploadFile(
        file=io.BytesIO(data), filename=name, headers=Headers({"content-type": "image/jpeg"})
    )


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
    verdicts((Decision.ACCEPT, 0.97), (Decision.REJECT, 0.03))

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
async def test_one_non_plant_photo_refuses_the_whole_submission(
    async_client: AsyncClient, uploads: list[dict[str, Any]], verdicts: Any
) -> None:
    """Regression: a plant photo used to carry a non-plant one through.

    Only an all-negative set was refused, so a non-plant first photo followed
    by a plant was accepted. Every photo has to pass on its own now.
    """
    verdicts((Decision.REJECT, 0.01), (Decision.ACCEPT, 0.98))

    response = await _post(async_client, _file("bad.jpg"), _file("good.jpg"))

    assert response.status_code == 422
    assert "Photo 1 of 2 does not appear to show a plant" in response.json()["message"]
    assert uploads == [], "the plant photo is not kept either"


@pytest.mark.asyncio
async def test_the_position_of_the_bad_photo_does_not_matter(
    async_client: AsyncClient, uploads: list[dict[str, Any]], verdicts: Any
) -> None:
    """The primary photo is not special: a bad last photo refuses just the same."""
    verdicts((Decision.ACCEPT, 0.98), (Decision.ACCEPT, 0.95), (Decision.REJECT, 0.04))

    response = await _post(async_client, _file("a.jpg"), _file("b.jpg"), _file("c.jpg"))

    assert response.status_code == 422
    assert "Photo 3 of 3" in response.json()["message"]
    assert uploads == []


@pytest.mark.asyncio
async def test_every_bad_photo_is_named_in_one_refusal(
    async_client: AsyncClient, uploads: list[dict[str, Any]], verdicts: Any
) -> None:
    """Screening finishes the set, so the contributor fixes it in one retry."""
    seen = verdicts((Decision.REJECT, 0.02), (Decision.ACCEPT, 0.97), (Decision.REJECT, 0.05))

    response = await _post(async_client, _file("a.jpg"), _file("b.jpg"), _file("c.jpg"))

    assert response.status_code == 422
    message = response.json()["message"]
    assert "Photos 1 and 3 of 3 do not appear to show a plant" in message
    assert "replace them" in message
    assert len(seen) == 3, "every photo was scored before deciding"


@pytest.mark.asyncio
async def test_an_uncertain_photo_among_plants_is_not_a_refusal(
    async_client: AsyncClient, uploads: list[dict[str, Any]], verdicts: Any
) -> None:
    """Only a confident negative refuses; the middle band stays for review.

    That band is where specimen shots and bark close-ups land. Refusing it
    would turn honest contributors away, since the model puts 7 of this
    project's own 104 curated plant photographs below its default cutoff.
    """
    verdicts((Decision.FLAG, 0.3), (Decision.ACCEPT, 0.98))

    response = await _post(async_client, _file("bark.jpg"), _file("leaf.jpg"))

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
    verdicts((Decision.ACCEPT, 0.97), (Decision.ACCEPT, 0.88))

    await _post(async_client, _file("a.jpg"), _file("b.jpg"))

    row = _isolated.added[0]
    assert row.ai_is_plant is True
    record = row.ai_top_matches["screening"]
    assert record["plant_probability"] == 0.88, "the weakest photo speaks for the submission"
    assert [p["plant_probability"] for p in record["photos"]] == [0.97, 0.88]
    # ai_top_matches is jsonb; anything not JSON-native fails at the driver.
    assert json.loads(json.dumps(row.ai_top_matches)) == row.ai_top_matches


@pytest.mark.asyncio
async def test_one_uncertain_photo_marks_the_submission_for_the_reviewer(
    async_client: AsyncClient, uploads: list[dict[str, Any]], verdicts: Any, _isolated: Any
) -> None:
    """Regression: a good photo used to vouch for an uncertain one.

    `ai_is_plant` was true if any photo was a confident plant, so a doubtful
    photo beside a clear one reached the reviewer marked as a plant.
    """
    verdicts((Decision.ACCEPT, 0.97), (Decision.FLAG, 0.42))

    response = await _post(async_client, _file("a.jpg"), _file("b.jpg"))

    assert response.status_code == 201
    row = _isolated.added[0]
    assert row.ai_is_plant is False
    assert row.ai_top_matches["screening"]["plant_probability"] == 0.42


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
async def test_a_photo_with_too_many_pixels_is_refused_not_a_500(
    async_client: AsyncClient, uploads: list[dict[str, Any]], monkeypatch: Any
) -> None:
    """A decompression bomb is the contributor's problem to fix, not a crash."""

    class _Stub:
        def classify(self, data: bytes) -> Verdict:
            raise ImageTooLargeError("12000x12000 is 144 megapixels; the limit is 13")

    monkeypatch.setattr(svc, "get_classifier", lambda: _Stub())

    response = await _post(async_client)

    assert response.status_code == 422
    assert "too large to process" in response.json()["message"]
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


# ── photos added to a pending submission ─────────────────────────────────────


class _PendingSession(FakeSession):
    """Enough AsyncSession for `add_photos` on a submission still pending.

    `record` is the submission's stored `ai_top_matches`. Writes to the row
    are captured in `updates`, as the values they set.
    """

    def __init__(self, record: dict[str, Any] | None) -> None:
        super().__init__()
        self.record = record
        self.updates: list[dict[str, Any]] = []

    async def execute(self, statement: Any) -> Any:
        if isinstance(statement, Update):
            self.updates.append(statement.compile(dialect=postgresql.dialect()).params)
            return None

        class _Pending:
            def first(self) -> tuple[Any, ...]:
                return (uuid4(),)

        return _Pending()

    async def scalar(self, statement: Any) -> Any:
        # add_photos counts the photos already attached; folding in the new
        # verdicts reads the stored record.
        return 1 if "count(" in str(statement) else self.record

    def expire_all(self) -> None:
        return None


def _record(*verdicts: tuple[Decision, float]) -> dict[str, Any]:
    return svc.Screening(
        per_photo=[Verdict(decision=d, confidence=c) for d, c in verdicts]
    ).as_record()


@pytest.fixture
def adding(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A service adding photos to a pending submission with a given record."""

    async def _get_by_id(self: Any, submission_id: Any) -> Any:
        return object()

    monkeypatch.setattr(svc.SubmissionService, "get_by_id", _get_by_id)

    def _build(record: dict[str, Any] | None) -> tuple[svc.SubmissionService, _PendingSession]:
        session = _PendingSession(record)
        return svc.SubmissionService(session), session  # type: ignore[arg-type]

    return _build


@pytest.mark.asyncio
async def test_a_non_plant_photo_cannot_be_added_later(
    uploads: list[dict[str, Any]], verdicts: Any, adding: Any
) -> None:
    """Regression: added photos were never screened.

    Submitting a plant and then adding anything at all walked straight past
    the refusal on create.
    """
    verdicts((Decision.REJECT, 0.02))
    service, session = adding(_record((Decision.ACCEPT, 0.98)))

    with pytest.raises(svc.NotAPlantError, match="This photo does not appear"):
        await service.add_photos(uuid4(), [_upload(_jpeg())])
    assert uploads == [], "a refused photo must not reach the bucket"
    assert session.added == [] and session.updates == []


@pytest.mark.asyncio
async def test_an_uncertain_photo_added_later_marks_the_submission(
    uploads: list[dict[str, Any]], verdicts: Any, adding: Any
) -> None:
    """The stored verdict ends up as if the photo had been sent with the rest."""
    verdicts((Decision.FLAG, 0.4))
    service, session = adding(_record((Decision.ACCEPT, 0.98)))

    await service.add_photos(uuid4(), [_upload(_jpeg())])

    assert len(uploads) == 1
    (written,) = session.updates
    assert written["ai_is_plant"] is False
    record = written["ai_top_matches"]["screening"]
    assert [p["plant_probability"] for p in record["photos"]] == [0.98, 0.4]
    assert record["plant_probability"] == 0.4


@pytest.mark.asyncio
async def test_photos_added_to_an_unscreened_submission_leave_it_unscreened(
    uploads: list[dict[str, Any]], verdicts: Any, adding: Any
) -> None:
    """The earlier photos were never looked at, so no verdict is invented."""
    verdicts((Decision.ACCEPT, 0.99))
    service, session = adding(None)

    await service.add_photos(uuid4(), [_upload(_jpeg())])

    assert len(uploads) == 1
    assert session.updates == []


# ── bounded concurrency ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_so_many_photos_are_decoded_at_once(monkeypatch: Any) -> None:
    """Decoding is where the memory goes, so it has to be bounded.

    One 1919x2560 photograph costs about 70 MB while being decoded, and Cloud
    Run puts 80 concurrent requests on an instance by default. Unbounded that
    is several GB: the instance is killed and every request in flight dies
    with it, including the ones that were nearly done.
    """
    import asyncio

    monkeypatch.setattr(settings, "PLANT_SCREENING_CONCURRENCY", 2)
    svc.reset_screening_limit()

    in_flight = 0
    high_water = 0

    class _Slow:
        def classify(self, data: bytes) -> Verdict:
            nonlocal in_flight, high_water
            in_flight += 1
            high_water = max(high_water, in_flight)
            time.sleep(0.02)
            in_flight -= 1
            return Verdict(decision=Decision.ACCEPT, confidence=0.9)

    monkeypatch.setattr(svc, "get_classifier", lambda: _Slow())

    service = svc.SubmissionService(FakeSession())  # type: ignore[arg-type]
    await asyncio.gather(*(service.screen([_upload(_jpeg())]) for _ in range(6)))

    assert high_water <= 2, f"{high_water} photos were decoded at once, limit was 2"
    svc.reset_screening_limit()
