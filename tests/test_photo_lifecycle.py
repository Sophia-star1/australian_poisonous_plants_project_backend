"""Tests for photo storage across add and delete on an existing submission.

These cover two contract mismatches the merge of the storage work into the
owner-edit work produced. Both compiled and both passed the existing suites,
because each branch stubbed the method the other one changed:

* `add_photos` called `_store_photo(photo)` after the signature had gained a
  `submission_id`, so every added photo would have raised at runtime;
* `delete_photo` passed a public URL where the storage API wants a key —
  harmless while `delete_file` ignored its argument, wrong once it performs a
  real request.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.config import settings
from app.models.submission import Submission, SubmissionImage
from app.services.storage_service import StoredFile
from app.services.submission_service import SubmissionService

SUBMISSION_ID = UUID("44444444-4444-4444-4444-444444444444")
PHOTO_ID = UUID("55555555-5555-5555-5555-555555555555")
PUBLIC_URL = (
    "https://proj.supabase.co/storage/v1/object/public/"
    "submission-images/submissions/44444444-4444-4444-4444-444444444444/abc.jpg"
)


class FakeUpload:
    """Enough of UploadFile for the service to work with."""

    def __init__(self, filename: str = "IMG_0001.jpg") -> None:
        self.filename = filename
        self.content_type = "image/jpeg"


class FakeSession:
    """Records writes; answers the guard query as 'still pending'."""

    def __init__(self, *, image: SubmissionImage | None = None) -> None:
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.image = image
        self.pending = True
        self.statements: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        pending = self.pending

        class _Result:
            def first(self) -> tuple[Any, ...] | None:
                return (SUBMISSION_ID,) if pending else None

        return _Result()

    async def scalar(self, statement: Any) -> Any:
        # First call in add_photos counts existing images; delete_photo looks
        # up the image row. Returning the image when one is configured covers
        # both without the tests needing to model SQL.
        return self.image if self.image is not None else 0

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)

    async def flush(self) -> None:
        return None

    def expire_all(self) -> None:
        return None


def _submission() -> Submission:
    sub = Submission()
    sub.id = SUBMISSION_ID
    sub.status = "pending"
    sub.images = []
    return sub


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build a service whose storage calls are recorded rather than made."""

    def _build(session: FakeSession) -> tuple[SubmissionService, dict[str, Any]]:
        calls: dict[str, Any] = {"uploads": [], "deletes": []}

        async def _upload_file(
            self: Any, file: Any, key: str, bucket: str | None = None
        ) -> StoredFile:
            calls["uploads"].append({"key": key, "bucket": bucket})
            return StoredFile(key=key, url=f"https://proj.supabase.co/x/{key}")

        async def _delete_file(self: Any, key: str, bucket: str | None = None) -> bool:
            calls["deletes"].append({"key": key, "bucket": bucket})
            return True

        async def _get_by_id(self: Any, submission_id: UUID) -> Submission:
            return _submission()

        monkeypatch.setattr(
            "app.services.submission_service.StorageService.upload_file", _upload_file
        )
        monkeypatch.setattr(
            "app.services.submission_service.StorageService.delete_file", _delete_file
        )
        monkeypatch.setattr(SubmissionService, "get_by_id", _get_by_id)
        # FakeUpload carries no bytes, and screening added photos is covered
        # in test_submission_screening.py; these tests are about storage keys.
        monkeypatch.setattr("app.services.submission_service.get_classifier", lambda: None)
        return SubmissionService(session), calls  # type: ignore[arg-type]

    return _build


@pytest.mark.asyncio
async def test_added_photos_are_keyed_by_submission(service: Any) -> None:
    """Regression: add_photos called _store_photo with the old signature."""
    svc, calls = service(FakeSession())
    await svc.add_photos(SUBMISSION_ID, [FakeUpload(), FakeUpload()])

    assert len(calls["uploads"]) == 2
    for upload in calls["uploads"]:
        assert upload["key"].startswith(f"submissions/{SUBMISSION_ID}/")
        assert upload["bucket"] == settings.SUPABASE_SUBMISSION_BUCKET
    assert calls["uploads"][0]["key"] != calls["uploads"][1]["key"]


@pytest.mark.asyncio
async def test_added_photo_filename_never_becomes_the_key(service: Any) -> None:
    """Same collision rule as the create path."""
    svc, calls = service(FakeSession())
    await svc.add_photos(SUBMISSION_ID, [FakeUpload("IMG_0001.jpg")])
    assert "IMG_0001" not in calls["uploads"][0]["key"]


@pytest.mark.asyncio
async def test_add_photos_refuses_a_reviewed_submission(service: Any) -> None:
    """The database guard, not the row we read earlier, decides."""
    session = FakeSession()
    session.pending = False
    svc, calls = service(session)

    assert await svc.add_photos(SUBMISSION_ID, [FakeUpload()]) is None
    assert calls["uploads"] == [], "nothing should be uploaded for a frozen submission"


@pytest.mark.asyncio
async def test_deleted_photo_is_addressed_by_key_not_url(service: Any) -> None:
    """Regression: the row holds a URL, the storage API wants a key."""
    image = SubmissionImage()
    image.id = PHOTO_ID
    image.submission_id = SUBMISSION_ID
    image.image_url = PUBLIC_URL
    image.is_primary = False

    svc, calls = service(FakeSession(image=image))
    await svc.delete_photo(SUBMISSION_ID, PHOTO_ID)

    assert len(calls["deletes"]) == 1
    deleted = calls["deletes"][0]
    assert deleted["key"] == f"submissions/{SUBMISSION_ID}/abc.jpg"
    assert not deleted["key"].startswith("http"), "a URL was passed as a key"
    assert deleted["bucket"] == settings.SUPABASE_SUBMISSION_BUCKET


@pytest.mark.asyncio
async def test_deleting_a_legacy_row_does_not_crash(service: Any) -> None:
    """Rows written before storage existed hold a bare path, not a URL."""
    image = SubmissionImage()
    image.id = PHOTO_ID
    image.submission_id = SUBMISSION_ID
    image.image_url = "submissions/legacy.jpg"
    image.is_primary = False

    svc, calls = service(FakeSession(image=image))
    await svc.delete_photo(SUBMISSION_ID, PHOTO_ID)
    assert calls["deletes"][0]["key"] == "submissions/legacy.jpg"


def _guard_sql(session: FakeSession) -> str:
    """The first statement a photo change runs: its status guard."""
    return str(session.statements[0].compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_adding_photos_waits_for_a_review_in_progress(service: Any) -> None:
    """The guard locks the row a reviewer holds, instead of reading past it.

    A plain SELECT does not wait for a lock, so a photo added while a review
    was being decided slipped in after the reviewer's version check.
    """
    session = FakeSession()
    svc, _ = service(session)
    await svc.add_photos(SUBMISSION_ID, [FakeUpload()])
    assert _guard_sql(session).rstrip().endswith("FOR UPDATE")


@pytest.mark.asyncio
async def test_deleting_a_photo_waits_for_a_review_in_progress(service: Any) -> None:
    image = SubmissionImage()
    image.id = PHOTO_ID
    image.submission_id = SUBMISSION_ID
    image.image_url = PUBLIC_URL
    image.is_primary = False
    session = FakeSession(image=image)
    svc, _ = service(session)
    await svc.delete_photo(SUBMISSION_ID, PHOTO_ID)
    assert _guard_sql(session).rstrip().endswith("FOR UPDATE")


@pytest.mark.asyncio
async def test_delete_refuses_a_reviewed_submission(service: Any) -> None:
    """Frozen means frozen for photos too."""
    session = FakeSession(image=SubmissionImage())
    session.pending = False
    svc, calls = service(session)

    assert await svc.delete_photo(SUBMISSION_ID, uuid4()) is None
    assert calls["deletes"] == []
