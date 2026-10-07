"""Tests for contributor editing of their own submissions.

Rules under test:

* only the contributor may edit, and only while the submission is pending;
* once reviewed it is readable but frozen;
* a stranger cannot tell an existing submission from a missing one;
* the write is guarded in the database, so a reviewer approving mid-edit wins.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.api.v1 import submissions as api
from app.dependencies import get_current_active_user
from app.main import app
from app.models.user import User
from app.schemas.submission import MAX_PHOTOS_PER_SUBMISSION, SubmissionContentUpdate
from app.services.submission_service import NotAPlantError

OWNER_EMAIL = "owner@example.com"
SUBMISSION_ID = UUID("11111111-1111-1111-1111-111111111111")
PHOTO_ID = UUID("22222222-2222-2222-2222-222222222222")


class FakeImage:
    """Stand-in for a SubmissionImage row."""

    def __init__(self, image_id: UUID = PHOTO_ID, is_primary: bool = True) -> None:
        self.id = image_id
        self.image_url = "submissions/p.jpg"
        self.is_primary = is_primary


class FakeSubmission:
    """Stand-in for a Submission row, shaped for SubmissionOut."""

    def __init__(self, status: str = "pending", email: str = OWNER_EMAIL) -> None:
        self.id = SUBMISSION_ID
        self.contributor_name = "Owner"
        self.contributor_email = email
        self.user_description = "original"
        self.location_state = "NSW"
        self.latitude = None
        self.longitude = None
        self.status = status
        self.ai_is_plant = None
        self.ai_top_matches = None
        self.reviewer_notes = None
        self.reviewed_by = None
        self.reviewed_at = None
        self.submitted_at = datetime.now(UTC)
        self.images = [FakeImage()]


def _user(email: str = OWNER_EMAIL, role: str = "user") -> User:
    user = User()
    user.id = 1
    user.email = email
    user.full_name = "Owner"
    user.is_active = True
    user.role = role
    return user


@pytest.fixture
def as_user() -> Any:
    """Authenticate requests as an arbitrary user without a database."""

    def _apply(user: User) -> None:
        app.dependency_overrides[get_current_active_user] = lambda: user

    yield _apply
    app.dependency_overrides.pop(get_current_active_user, None)


@pytest.fixture
def stub_service(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace SubmissionService methods; records calls for assertions."""
    calls: dict[str, Any] = {}

    def _apply(
        row: FakeSubmission | None,
        *,
        update_result: FakeSubmission | None = "unset",  # type: ignore[assignment]
        photo_count: int = 1,
    ) -> dict[str, Any]:
        async def _get_by_id(self: Any, submission_id: UUID) -> Any:
            return row

        async def _update_content(self: Any, submission_id: UUID, payload: Any) -> Any:
            calls["update_payload"] = payload
            return row if update_result == "unset" else update_result

        async def _add_photos(self: Any, submission_id: UUID, photos: Any) -> Any:
            calls["added"] = len(photos)
            return row if update_result == "unset" else update_result

        async def _delete_photo(self: Any, submission_id: UUID, photo_id: UUID) -> Any:
            calls["deleted"] = photo_id
            return row if update_result == "unset" else update_result

        async def _count_photos(self: Any, submission_id: UUID) -> int:
            return photo_count

        monkeypatch.setattr(api.SubmissionService, "get_by_id", _get_by_id)
        monkeypatch.setattr(api.SubmissionService, "update_content", _update_content)
        monkeypatch.setattr(api.SubmissionService, "add_photos", _add_photos)
        monkeypatch.setattr(api.SubmissionService, "delete_photo", _delete_photo)
        monkeypatch.setattr(api.SubmissionService, "count_photos", _count_photos)
        return calls

    return _apply


def _photo_file() -> dict[str, Any]:
    return {"photos": ("new.jpg", b"bytes", "image/jpeg")}


# ── viewing ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_can_read_own_submission(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """The read-only view a contributor keeps after review."""
    as_user(_user())
    stub_service(FakeSubmission(status="approved"))

    response = await async_client.get(f"/api/v1/submissions/{SUBMISSION_ID}")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "approved"


@pytest.mark.asyncio
async def test_stranger_gets_404_not_403(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """Answering 'forbidden' would confirm the id exists."""
    as_user(_user(email="someone.else@example.com"))
    stub_service(FakeSubmission())

    response = await async_client.get(f"/api/v1/submissions/{SUBMISSION_ID}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_admin_can_read_any_submission(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """Reviewers still need to see everything."""
    as_user(_user(email="admin@example.com", role="admin"))
    stub_service(FakeSubmission())

    assert (await async_client.get(f"/api/v1/submissions/{SUBMISSION_ID}")).status_code == 200


@pytest.mark.asyncio
async def test_missing_submission_is_404(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """An unknown id behaves identically to someone else's."""
    as_user(_user())
    stub_service(None)

    assert (await async_client.get(f"/api/v1/submissions/{SUBMISSION_ID}")).status_code == 404


@pytest.mark.asyncio
async def test_reading_requires_authentication(async_client: AsyncClient) -> None:
    """Submissions are not public."""
    assert (await async_client.get(f"/api/v1/submissions/{SUBMISSION_ID}")).status_code == 401


# ── editing text fields ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_can_edit_pending_submission(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """The core of the feature."""
    as_user(_user())
    calls = stub_service(FakeSubmission())

    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}/content",
        json={"user_description": "corrected description"},
    )
    assert response.status_code == 200
    assert calls["update_payload"].user_description == "corrected description"


@pytest.mark.asyncio
async def test_editing_a_reviewed_submission_is_409(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """Approved content must stay as the reviewer saw it."""
    as_user(_user())
    stub_service(FakeSubmission(status="approved"))

    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}/content",
        json={"user_description": "sneaky change"},
    )
    assert response.status_code == 409
    assert "already been approved" in response.json()["message"]


@pytest.mark.asyncio
async def test_rejected_submission_is_also_frozen(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """Rejection is a decision too; editing would silently reopen it."""
    as_user(_user())
    stub_service(FakeSubmission(status="rejected"))

    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}/content", json={"location_state": "VIC"}
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_admin_cannot_rewrite_someone_elses_submission(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """Reviewing and authoring stay separate powers."""
    as_user(_user(email="admin@example.com", role="admin"))
    stub_service(FakeSubmission())

    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}/content", json={"user_description": "edited"}
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_concurrent_review_during_edit_returns_409(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """The conditional UPDATE matched zero rows: a reviewer got there first."""
    as_user(_user())
    stub_service(FakeSubmission(), update_result=None)

    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}/content", json={"user_description": "late"}
    )
    assert response.status_code == 409
    assert "reviewed while you were editing" in response.json()["message"]


@pytest.mark.asyncio
async def test_status_cannot_be_changed_through_the_content_endpoint(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """extra='forbid' keeps self-approval out of reach."""
    as_user(_user())
    stub_service(FakeSubmission())

    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}/content", json={"status": "approved"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_contributor_email_cannot_be_reassigned(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """Ownership is derived from the email, so it is not an editable field."""
    as_user(_user())
    stub_service(FakeSubmission())

    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}/content",
        json={"contributor_email": "victim@example.com"},
    )
    assert response.status_code == 422


def test_omitted_fields_are_not_applied() -> None:
    """A partial edit must not blank the fields it did not mention."""
    payload = SubmissionContentUpdate.model_validate({"user_description": "only this"})
    assert payload.model_dump(exclude_unset=True) == {"user_description": "only this"}


def test_explicit_null_is_distinguishable_from_omission() -> None:
    """Sending null clears the field on purpose; omitting it does not."""
    payload = SubmissionContentUpdate.model_validate({"location_state": None})
    assert payload.model_dump(exclude_unset=True) == {"location_state": None}


# ── photos ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_can_add_a_photo(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """Adding to a pending submission works."""
    as_user(_user())
    calls = stub_service(FakeSubmission(), photo_count=1)

    response = await async_client.post(
        f"/api/v1/submissions/{SUBMISSION_ID}/photos", files=_photo_file()
    )
    assert response.status_code == 201
    assert calls["added"] == 1


@pytest.mark.asyncio
async def test_photo_limit_is_enforced(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """A submission cannot grow without bound."""
    as_user(_user())
    stub_service(FakeSubmission(), photo_count=MAX_PHOTOS_PER_SUBMISSION)

    response = await async_client.post(
        f"/api/v1/submissions/{SUBMISSION_ID}/photos", files=_photo_file()
    )
    assert response.status_code == 422
    assert "at most" in response.json()["message"]


@pytest.mark.asyncio
async def test_a_photo_screening_refuses_is_a_422_when_adding(
    async_client: AsyncClient, as_user: Any, stub_service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same refusal, in the same words, as when creating the submission."""
    as_user(_user())
    stub_service(FakeSubmission(), photo_count=1)

    async def _refuse(self: Any, submission_id: UUID, photos: Any) -> Any:
        raise NotAPlantError([(1, 0.02)], total=1)

    monkeypatch.setattr(api.SubmissionService, "add_photos", _refuse)

    response = await async_client.post(
        f"/api/v1/submissions/{SUBMISSION_ID}/photos", files=_photo_file()
    )
    assert response.status_code == 422
    assert "does not appear to show a plant" in response.json()["message"]


@pytest.mark.asyncio
async def test_cannot_add_photos_after_review(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """Same freeze rule as the text fields."""
    as_user(_user())
    stub_service(FakeSubmission(status="approved"))

    response = await async_client.post(
        f"/api/v1/submissions/{SUBMISSION_ID}/photos", files=_photo_file()
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_owner_can_delete_a_photo_when_others_remain(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """Deleting is allowed while more than one photo is attached."""
    as_user(_user())
    calls = stub_service(FakeSubmission(), photo_count=2)

    response = await async_client.delete(
        f"/api/v1/submissions/{SUBMISSION_ID}/photos/{PHOTO_ID}"
    )
    assert response.status_code == 200
    assert calls["deleted"] == PHOTO_ID


@pytest.mark.asyncio
async def test_last_photo_cannot_be_deleted(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """A submission with no image is useless to a reviewer."""
    as_user(_user())
    stub_service(FakeSubmission(), photo_count=1)

    response = await async_client.delete(
        f"/api/v1/submissions/{SUBMISSION_ID}/photos/{PHOTO_ID}"
    )
    assert response.status_code == 422
    assert "at least one photo" in response.json()["message"]


@pytest.mark.asyncio
async def test_deleting_an_unknown_photo_is_404(
    async_client: AsyncClient, as_user: Any, stub_service: Any
) -> None:
    """The photo id must belong to this submission."""
    as_user(_user())
    stub_service(FakeSubmission(), update_result=None, photo_count=3)

    response = await async_client.delete(
        f"/api/v1/submissions/{SUBMISSION_ID}/photos/{uuid4()}"
    )
    assert response.status_code == 404
