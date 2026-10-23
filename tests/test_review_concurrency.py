"""Tests for the contributor and the reviewer working on the same submission.

Both may act at once: the contributor can edit while pending, and the reviewer
decides from what they were shown. The danger is not a corrupt write — every
mutation is already a conditional UPDATE guarded on `status = 'pending'`, so
whoever commits first wins. It is that a reviewer approves *content they never
saw*, most importantly a photograph swapped out while they were reading.

`content_version` closes that. It is a digest of what the reviewer looks at,
returned with the submission and required back when deciding. Nothing is
locked, so neither side is ever blocked or left holding a stale claim; the one
who acts second is told what happened and can look again.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.dialects import postgresql

from app.db.session import get_db
from app.dependencies import get_current_active_user
from app.main import app
from app.models.plant import Plant
from app.models.submission import Submission, SubmissionImage
from app.models.user import User
from app.schemas.submission import SubmissionOut, content_fingerprint
from app.services.submission_service import SubmissionService, submission_fingerprint

SUBMISSION_ID = UUID("55555555-5555-5555-5555-555555555555")
PHOTO_A = UUID("aaaaaaaa-0000-0000-0000-000000000001")
PHOTO_B = UUID("bbbbbbbb-0000-0000-0000-000000000002")


def _submission(photos: tuple[UUID, ...] = (PHOTO_A, PHOTO_B)) -> Submission:
    row = Submission()
    row.id = SUBMISSION_ID
    row.contributor_name = "Ada"
    row.contributor_email = "ada@example.com"
    row.user_description = "beside the creek"
    row.location_state = "NSW"
    row.latitude = Decimal("-33.8")
    row.longitude = Decimal("151.2")
    row.status = "pending"
    row.ai_is_plant = None
    row.ai_top_matches = None
    row.reviewer_notes = None
    row.reviewed_by = None
    row.reviewed_at = None
    row.submitted_at = datetime.now(UTC)
    row.images = []
    for i, pid in enumerate(photos):
        image = SubmissionImage()
        image.id = pid
        image.submission_id = SUBMISSION_ID
        image.image_url = f"https://proj.supabase.co/x/{pid}.jpg"
        image.is_primary = i == 0
        row.images.append(image)
    return row


# ── the digest itself ────────────────────────────────────────────────────────


def test_the_same_content_gives_the_same_version() -> None:
    assert submission_fingerprint(_submission()) == submission_fingerprint(_submission())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_description", "actually beside the river"),
        ("contributor_name", "Ada L."),
        ("location_state", "QLD"),
        ("latitude", Decimal("-34.0")),
        ("longitude", Decimal("150.0")),
    ],
)
def test_every_editable_field_moves_the_version(field: str, value: Any) -> None:
    """Whatever a contributor can change, a reviewer must be told changed."""
    before = _submission()
    after = _submission()
    setattr(after, field, value)
    assert submission_fingerprint(before) != submission_fingerprint(after)


def test_swapping_a_photo_moves_the_version() -> None:
    """The case this exists for.

    Text edits are visible in the response a reviewer reads. A replaced
    photograph is the thing that could be approved without anyone noticing.
    """
    before = _submission(photos=(PHOTO_A, PHOTO_B))
    after = _submission(photos=(PHOTO_A, uuid4()))
    assert submission_fingerprint(before) != submission_fingerprint(after)


def test_removing_a_photo_moves_the_version() -> None:
    assert submission_fingerprint(_submission(photos=(PHOTO_A,))) != submission_fingerprint(
        _submission(photos=(PHOTO_A, PHOTO_B))
    )


def test_changing_which_photo_is_primary_moves_the_version() -> None:
    """The primary is the one shown first, so it is part of what was seen."""
    after = _submission()
    after.images[0].is_primary = False
    after.images[1].is_primary = True
    assert submission_fingerprint(_submission()) != submission_fingerprint(after)


def test_photo_order_alone_does_not_move_the_version() -> None:
    """Row order is not a change; the database makes no promise about it."""
    reordered = _submission()
    reordered.images.reverse()
    assert submission_fingerprint(_submission()) == submission_fingerprint(reordered)


def test_the_reviewers_own_fields_are_not_part_of_it() -> None:
    """Otherwise a reviewer's own write would invalidate the version they hold."""
    after = _submission()
    after.reviewer_notes = "looks right"
    after.reviewed_by = "reviewer@example.com"
    after.status = "approved"
    assert submission_fingerprint(_submission()) == submission_fingerprint(after)


def test_equivalent_coordinates_are_not_a_change() -> None:
    """-33.80 and -33.8 are the same place."""
    padded = _submission()
    padded.latitude = Decimal("-33.80")
    assert submission_fingerprint(_submission()) == submission_fingerprint(padded)


def test_the_response_and_the_check_agree() -> None:
    """One implementation, fed from the schema and from the ORM.

    Two would drift, and the drift would look like a phantom edit that no
    reload could clear.
    """
    row = _submission()
    assert SubmissionOut.model_validate(row).content_version == submission_fingerprint(row)


def test_the_version_is_short_enough_to_pass_around() -> None:
    assert len(submission_fingerprint(_submission())) == 16


def test_fields_cannot_bleed_into_each_other() -> None:
    """Concatenation without a separator would make ("ab","c") match ("a","bc")."""
    first = content_fingerprint(
        contributor_name="ab",
        user_description="c",
        location_state=None,
        latitude=None,
        longitude=None,
        photos=[],
    )
    second = content_fingerprint(
        contributor_name="a",
        user_description="bc",
        location_state=None,
        latitude=None,
        longitude=None,
        photos=[],
    )
    assert first != second


# ── the endpoint ─────────────────────────────────────────────────────────────


class FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, Plant) and obj.id is None:
            obj.id = 7

    async def get(self, model: type, pk: Any) -> Any:
        return next((o for o in self.added if isinstance(o, model)), None)

    async def execute(self, statement: Any) -> Any:
        class _Result:
            def first(self) -> tuple[Any, ...]:
                return (SUBMISSION_ID,)

        return _Result()

    async def scalar(self, statement: Any) -> Any:
        """The duplicate-name lookup: no plant by that name yet."""
        return None

    async def flush(self) -> None:
        for obj in self.added:
            if isinstance(obj, Plant) and obj.id is None:
                obj.id = 7

    def expire_all(self) -> None:
        return None

    async def refresh(self, obj: Any, attribute_names: list[str] | None = None) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.fixture
def reviewing(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Serve one submission to the review endpoint and record what it writes."""
    session = FakeSession()

    def _apply(row: Submission) -> FakeSession:
        async def _get_by_id(self: Any, submission_id: UUID) -> Submission:
            return row

        async def _copy_object(self: Any, **kwargs: Any) -> Any:
            from app.services.storage_service import StoredFile

            return StoredFile(key="k", url="https://proj.supabase.co/x/k.jpg")

        monkeypatch.setattr(SubmissionService, "get_by_id", _get_by_id)
        monkeypatch.setattr(
            "app.services.submission_service.StorageService.copy_object", _copy_object
        )

        async def _fake_db() -> AsyncIterator[FakeSession]:
            yield session

        def _admin() -> User:
            user = User()
            user.id = 1
            user.email = "reviewer@example.com"
            user.full_name = "Reviewer"
            user.is_active = True
            user.role = "admin"
            return user

        app.dependency_overrides[get_db] = _fake_db
        app.dependency_overrides[get_current_active_user] = _admin
        return session

    yield _apply
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_active_user, None)


def _body(version: str) -> dict[str, Any]:
    return {
        "status": "approved",
        "expected_version": version,
        "plant": {"botanical_name": "Abrus precatorius"},
    }


@pytest.mark.asyncio
async def test_a_current_version_is_accepted(async_client: AsyncClient, reviewing: Any) -> None:
    row = _submission()
    reviewing(row)

    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}", json=_body(submission_fingerprint(row))
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_a_stale_version_is_refused(async_client: AsyncClient, reviewing: Any) -> None:
    """The reviewer loaded it, the contributor edited, the reviewer decided."""
    row = _submission()
    stale = submission_fingerprint(row)
    row.user_description = "edited while the reviewer was reading"
    reviewing(row)

    response = await async_client.patch(f"/api/v1/submissions/{SUBMISSION_ID}", json=_body(stale))

    assert response.status_code == 409
    assert "edited since you opened it" in response.json()["message"]


@pytest.mark.asyncio
async def test_a_stale_review_publishes_nothing(async_client: AsyncClient, reviewing: Any) -> None:
    """A refusal has to happen before the plant is created, not after."""
    row = _submission()
    stale = submission_fingerprint(row)
    row.images.pop()
    session = reviewing(row)

    await async_client.patch(f"/api/v1/submissions/{SUBMISSION_ID}", json=_body(stale))

    assert [o for o in session.added if isinstance(o, Plant)] == []


@pytest.mark.asyncio
async def test_reloading_lets_the_reviewer_continue(
    async_client: AsyncClient, reviewing: Any
) -> None:
    """The way out of a 409 is to look again, not to retry harder."""
    row = _submission()
    row.user_description = "edited while the reviewer was reading"
    reviewing(row)

    fresh = (await async_client.get(f"/api/v1/submissions/{SUBMISSION_ID}")).json()
    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}", json=_body(fresh["data"]["content_version"])
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_the_version_is_returned_with_the_submission(
    async_client: AsyncClient, reviewing: Any
) -> None:
    """A reviewer cannot send back something they were never given."""
    row = _submission()
    reviewing(row)

    body = (await async_client.get(f"/api/v1/submissions/{SUBMISSION_ID}")).json()["data"]
    assert body["content_version"] == submission_fingerprint(row)


@pytest.mark.asyncio
async def test_the_version_cannot_be_omitted(async_client: AsyncClient, reviewing: Any) -> None:
    """A guard a client can forget to send is not a guard."""
    reviewing(_submission())

    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json={"status": "approved", "plant": {"botanical_name": "X"}},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_a_rejection_is_guarded_too(async_client: AsyncClient, reviewing: Any) -> None:
    """Rejecting is also a decision about content, and also final."""
    row = _submission()
    stale = submission_fingerprint(row)
    row.user_description = "changed"
    reviewing(row)

    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json={"status": "rejected", "expected_version": stale},
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_the_row_is_locked_before_the_version_is_checked(
    async_client: AsyncClient, reviewing: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an edit landing mid-review was approved unseen.

    The guarded UPDATE re-checks only the status, so a contributor's edit that
    committed between the version check and that UPDATE went through: the
    version matched what was read, not what was then written. The row is now
    locked first, so the edit waits for the review or the review sees it.
    """
    row = _submission()
    session = reviewing(row)
    steps: list[str] = []

    original_execute = session.execute

    async def _recording_execute(statement: Any) -> Any:
        steps.append(str(statement.compile(dialect=postgresql.dialect())))
        return await original_execute(statement)

    async def _recording_get_by_id(self: Any, submission_id: UUID) -> Submission:
        steps.append("read the submission")
        return row

    monkeypatch.setattr(session, "execute", _recording_execute)
    monkeypatch.setattr(SubmissionService, "get_by_id", _recording_get_by_id)

    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}", json=_body(submission_fingerprint(row))
    )

    assert response.status_code == 200, response.text
    lock = steps[0]
    assert "FROM submissions" in lock and lock.rstrip().endswith("FOR UPDATE")
    assert steps[1] == "read the submission", "locked before the version is read"
