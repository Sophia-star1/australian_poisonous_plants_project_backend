"""Photos uploaded by a request that does not land are deleted again.

The rows roll back with the transaction; the objects already in the bucket do
not, and nothing would ever reference or remove them. Three places that
happens: a later photo's upload fails, the flush fails, or the commit fails.

The commit is the subtle one. `get_db` commits after the response has gone
out, so a failure there used to reach nobody — the contributor had their 201.
The upload endpoints now commit before answering.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.config import settings
from app.db.session import get_db
from app.dependencies import get_current_active_user
from app.main import app
from app.ml.classifier import reset_classifier
from app.models.submission import Submission, SubmissionImage
from app.models.user import User
from app.services.storage_service import StorageError, StoredFile
from app.services.submission_service import SubmissionService

SUBMISSION_ID = UUID("66666666-6666-6666-6666-666666666666")


class FakeSession:
    """Records commits; fails flush or commit on request."""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.commits = 0
        self.flush_error: Exception | None = None
        self.commit_error: Exception | None = None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        if self.flush_error:
            raise self.flush_error
        for obj in self.added:
            if getattr(obj, "submitted_at", None) is None and isinstance(obj, Submission):
                obj.submitted_at = datetime.now(UTC)
            for image in getattr(obj, "images", []):
                if image.id is None:
                    image.id = uuid4()

    async def refresh(self, obj: Any, attribute_names: list[str] | None = None) -> None:
        return None

    async def commit(self) -> None:
        if self.commit_error:
            raise self.commit_error
        self.commits += 1

    async def rollback(self) -> None:
        return None


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A fake session and storage; `fail_upload_at` makes the Nth upload fail."""
    monkeypatch.setattr(settings, "PLANT_CLASSIFIER_ENABLED", False)
    reset_classifier()

    state: dict[str, Any] = {
        "session": FakeSession(),
        "uploaded": [],
        "deleted": [],
        "fail_upload_at": None,
    }

    async def _upload_file(self: Any, file: Any, key: str, bucket: str | None = None) -> Any:
        if len(state["uploaded"]) == state["fail_upload_at"]:
            raise StorageError("Upload failed with HTTP 500.")
        state["uploaded"].append(f"{bucket}/{key}")
        return StoredFile(key=key, url=f"https://proj.supabase.co/{bucket}/{key}")

    async def _delete_file(self: Any, key: str, bucket: str | None = None) -> bool:
        state["deleted"].append(f"{bucket}/{key}")
        return True

    monkeypatch.setattr(
        "app.services.submission_service.StorageService.upload_file", _upload_file
    )
    monkeypatch.setattr(
        "app.services.submission_service.StorageService.delete_file", _delete_file
    )

    async def _fake_db() -> AsyncIterator[FakeSession]:
        yield state["session"]

    app.dependency_overrides[get_db] = _fake_db
    yield state
    app.dependency_overrides.clear()


def _files(count: int) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("photos", (f"p{i}.jpg", b"bytes", "image/jpeg")) for i in range(count)]


async def _upload(client: AsyncClient, count: int = 3) -> Any:
    form = {"contributor_name": "Ada", "contributor_email": "ada@example.com"}
    return await client.post("/api/v1/submissions/", data=form, files=_files(count))


# ── creating a submission ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_later_upload_failing_removes_the_earlier_ones(
    async_client: AsyncClient, world: Any
) -> None:
    world["fail_upload_at"] = 2
    response = await _upload(async_client, 3)

    assert response.status_code == 422
    assert len(world["uploaded"]) == 2
    assert sorted(world["deleted"]) == sorted(world["uploaded"])


@pytest.mark.asyncio
async def test_a_failed_flush_removes_every_upload(async_client: AsyncClient, world: Any) -> None:
    world["session"].flush_error = RuntimeError("connection was closed")
    with pytest.raises(RuntimeError):
        await _upload(async_client, 3)
    assert len(world["uploaded"]) == 3
    assert sorted(world["deleted"]) == sorted(world["uploaded"])


@pytest.mark.asyncio
async def test_a_failed_commit_is_an_error_and_removes_every_upload(
    async_client: AsyncClient, world: Any
) -> None:
    """Used to be a 201 for a submission that was never saved."""
    world["session"].commit_error = RuntimeError("connection was closed")
    with pytest.raises(RuntimeError):
        await _upload(async_client, 2)
    assert len(world["uploaded"]) == 2
    assert sorted(world["deleted"]) == sorted(world["uploaded"])


@pytest.mark.asyncio
async def test_a_successful_upload_is_committed_before_the_answer_and_keeps_its_photos(
    async_client: AsyncClient, world: Any
) -> None:
    response = await _upload(async_client, 2)

    assert response.status_code == 201
    assert world["session"].commits >= 1
    assert world["deleted"] == []


# ── adding photos to one ─────────────────────────────────────────────────────


@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sign in as the contributor of a pending submission with one photo."""

    def _user() -> User:
        user = User()
        user.id = 1
        user.email = "ada@example.com"
        user.full_name = "Ada"
        user.is_active = True
        user.role = "user"
        return user

    async def _get_by_id(self: Any, submission_id: UUID) -> Submission:
        sub = Submission()
        sub.id = SUBMISSION_ID
        sub.contributor_name = "Ada"
        sub.contributor_email = "ada@example.com"
        sub.status = "pending"
        sub.submitted_at = datetime.now(UTC)
        image = SubmissionImage()
        image.id = uuid4()
        image.image_url = "https://proj.supabase.co/x/existing.jpg"
        image.is_primary = True
        sub.images = [image]
        return sub

    async def _count(self: Any, submission_id: UUID) -> int:
        return 1

    app.dependency_overrides[get_current_active_user] = _user
    monkeypatch.setattr(SubmissionService, "get_by_id", _get_by_id)
    monkeypatch.setattr(SubmissionService, "count_photos", _count)


async def _add(client: AsyncClient, count: int = 2) -> Any:
    return await client.post(f"/api/v1/submissions/{SUBMISSION_ID}/photos", files=_files(count))


@pytest.mark.asyncio
async def test_a_storage_failure_adding_photos_is_a_422_not_a_500(
    async_client: AsyncClient, world: Any, owner: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oversized files raise StorageError; only upload used to catch it."""

    async def _too_big(self: Any, submission_id: UUID, photos: Any) -> None:
        raise StorageError("File exceeds the 10 MB upload limit.")

    monkeypatch.setattr(SubmissionService, "add_photos", _too_big)
    response = await _add(async_client)

    assert response.status_code == 422
    assert "upload limit" in response.json()["message"]


@pytest.mark.asyncio
async def test_a_failed_commit_after_adding_photos_removes_them(
    async_client: AsyncClient, world: Any, owner: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _added(self: SubmissionService, submission_id: UUID, photos: Any) -> Any:
        self._uploaded_keys += ["submissions/x/new1.jpg", "submissions/x/new2.jpg"]
        return await SubmissionService.get_by_id(self, submission_id)

    monkeypatch.setattr(SubmissionService, "add_photos", _added)
    world["session"].commit_error = RuntimeError("connection was closed")

    with pytest.raises(RuntimeError):
        await _add(async_client)
    bucket = settings.SUPABASE_SUBMISSION_BUCKET
    assert sorted(world["deleted"]) == [
        f"{bucket}/submissions/x/new1.jpg",
        f"{bucket}/submissions/x/new2.jpg",
    ]


@pytest.mark.asyncio
async def test_add_photos_removes_its_uploads_when_one_fails(world: Any) -> None:
    """Service level: the second of two uploads fails."""

    class _Session(FakeSession):
        async def execute(self, statement: Any) -> Any:
            class _Pending:
                def first(self) -> tuple[Any, ...]:
                    return (SUBMISSION_ID,)

            return _Pending()

        async def scalar(self, statement: Any) -> int:
            return 1

    class _Upload:
        filename = "p.jpg"
        content_type = "image/jpeg"

    world["fail_upload_at"] = 1
    service = SubmissionService(_Session())  # type: ignore[arg-type]

    with pytest.raises(StorageError):
        await service.add_photos(SUBMISSION_ID, [_Upload(), _Upload()])  # type: ignore[list-item]
    assert len(world["uploaded"]) == 1
    assert world["deleted"] == world["uploaded"]

