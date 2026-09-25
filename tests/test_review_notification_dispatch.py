"""Tests for when the review endpoint sends its notification, and when it does not.

The content is covered in `test_review_notification.py`. What is pinned here
is ordering and blast radius:

* the message goes out **after** the transaction commits, never before;
* a review that failed sends nothing;
* a mail provider having a bad day cannot fail a review that already
  succeeded.

The ordering is a guarantee of FastAPI's, not of this code's — dependencies
with `yield` exit before the response is sent, background tasks run after it —
so it is asserted rather than assumed. If a future version reorders them, an
approval could be announced and then rolled back.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient

from app.api.v1 import submissions as api
from app.db.session import get_db
from app.dependencies import get_current_active_user
from app.main import app
from app.models.plant import Plant
from app.models.submission import Submission
from app.models.user import User
from app.services.notification_service import ReviewOutcome

SUBMISSION_ID = UUID("77777777-7777-7777-7777-777777777777")


def _submission(status: str = "approved") -> Submission:
    row = Submission()
    row.id = SUBMISSION_ID
    row.contributor_name = "Ada Lovelace"
    row.contributor_email = "ada@example.com"
    row.user_description = "beside the creek"
    row.location_state = "NSW"
    row.latitude = None
    row.longitude = None
    row.status = status
    row.ai_is_plant = None
    row.ai_top_matches = None
    row.reviewer_notes = "Clear photographs."
    row.reviewed_by = "reviewer@example.com"
    row.reviewed_at = datetime.now(UTC)
    row.submitted_at = datetime.now(UTC)
    row.images = []
    return row


def _plant() -> Plant:
    plant = Plant()
    plant.id = 42
    plant.botanical_name = "Abrus precatorius"
    plant.toxic_parts = ["seed"]
    plant.health_effects = "Vomiting and diarrhoea."
    plant.toxicity_by_species = {"humans": "Toxic"}
    return plant


@pytest.fixture
def review(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Wire the endpoint to fakes and record the order things happen in."""
    events: list[str] = []
    sent: list[ReviewOutcome] = []

    def _apply(
        *,
        status: str = "approved",
        plant: Plant | None = None,
        raises: Exception | None = None,
        delivered: bool = True,
    ) -> dict[str, Any]:
        async def _update_status(self: Any, submission_id: UUID, **kwargs: Any) -> Any:
            if raises is not None:
                raise raises
            return _submission(status), plant

        async def _notify(outcome: ReviewOutcome) -> bool:
            events.append("notify")
            sent.append(outcome)
            return delivered

        monkeypatch.setattr(api.SubmissionService, "update_status", _update_status)
        monkeypatch.setattr(api, "notify_review", _notify)

        class _RecordingSession:
            """Records the calls whose ordering the notification depends on."""

            async def commit(self) -> None:
                events.append("commit")

            async def rollback(self) -> None:  # pragma: no cover
                events.append("rollback")

        async def _fake_db() -> AsyncIterator[Any]:
            try:
                yield _RecordingSession()
            finally:
                # Stands in for `get_db` closing the session on the way out.
                events.append("teardown")

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
        return {"events": events, "sent": sent}

    yield _apply
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_active_user, None)


#: `update_status` is stubbed in this file, so the version is never actually
#: compared — but the field is required by the schema, and a review body
#: without it never reaches the endpoint at all.
STUB_VERSION = "0123456789abcdef"


def _approve() -> dict[str, Any]:
    return {
        "status": "approved",
        "expected_version": STUB_VERSION,
        "plant": {"botanical_name": "Abrus precatorius"},
    }


# ── ordering ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_message_goes_out_after_the_commit(
    async_client: AsyncClient, review: Any
) -> None:
    """Sending first would risk announcing a decision that then rolls back.

    The obvious reading of FastAPI's documentation is that `yield`
    dependencies exit before the response and background tasks run after it,
    which would put `get_db`'s commit first for free. Measured on FastAPI
    0.141 / Starlette 1.6 — against a real uvicorn server, not only the test
    transport — the order is the other way round: the background task runs
    first, and the dependency exits after it.

    So the endpoint commits explicitly before scheduling. This asserts the
    result rather than the framework's behaviour, so it keeps holding if a
    future version reorders them again.
    """
    state = review(plant=_plant())

    response = await async_client.patch(f"/api/v1/submissions/{SUBMISSION_ID}", json=_approve())

    assert response.status_code == 200
    assert state["events"] == ["commit", "notify", "teardown"]


@pytest.mark.asyncio
async def test_nothing_is_announced_before_it_is_written(
    async_client: AsyncClient, review: Any
) -> None:
    """The one ordering that must never invert."""
    state = review(plant=_plant())
    await async_client.patch(f"/api/v1/submissions/{SUBMISSION_ID}", json=_approve())
    assert state["events"].index("commit") < state["events"].index("notify")


# ── what is sent ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_approval_notifies_the_contributor(
    async_client: AsyncClient, review: Any
) -> None:
    state = review(plant=_plant())
    await async_client.patch(f"/api/v1/submissions/{SUBMISSION_ID}", json=_approve())

    outcome = state["sent"][0]
    assert outcome.contributor_email == "ada@example.com"
    assert outcome.status == "approved"
    assert outcome.botanical_name == "Abrus precatorius"
    assert outcome.reviewer_notes == "Clear photographs."


@pytest.mark.asyncio
async def test_a_rejection_notifies_too(async_client: AsyncClient, review: Any) -> None:
    """A rejection is the outcome someone is most anxious to hear about."""
    state = review(status="rejected", plant=None)
    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json={
            "status": "rejected",
            "expected_version": STUB_VERSION,
            "reviewer_notes": "Too blurred.",
        },
    )

    assert response.status_code == 200
    assert state["sent"][0].status == "rejected"
    assert state["sent"][0].botanical_name is None


@pytest.mark.asyncio
async def test_the_outcome_carries_no_orm_row(async_client: AsyncClient, review: Any) -> None:
    """The task runs after the session closes, so it must hold plain values.

    Passing the row itself would make the first attribute read happen on a
    detached instance, which raises under asyncio instead of loading.
    """
    state = review(plant=_plant())
    await async_client.patch(f"/api/v1/submissions/{SUBMISSION_ID}", json=_approve())

    outcome = state["sent"][0]
    assert isinstance(outcome, ReviewOutcome)
    assert not isinstance(outcome.contributor_email, Submission)
    assert isinstance(outcome.contributor_email, str)
    assert isinstance(outcome.botanical_name, str)


# ── nothing is sent for a review that did not happen ─────────────────────────


@pytest.mark.asyncio
async def test_a_second_reviewer_losing_the_race_notifies_nobody(
    async_client: AsyncClient, review: Any
) -> None:
    """409 means this reviewer changed nothing; the contributor already heard."""
    from app.services.submission_service import AlreadyReviewedError

    state = review(raises=AlreadyReviewedError("approved"))

    response = await async_client.patch(f"/api/v1/submissions/{SUBMISSION_ID}", json=_approve())

    assert response.status_code == 409
    assert state["sent"] == []
    assert "notify" not in state["events"]


@pytest.mark.asyncio
async def test_a_rejected_request_notifies_nobody(
    async_client: AsyncClient, review: Any
) -> None:
    """Approving without an identification never reaches the service."""
    state = review(plant=_plant())

    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json={"status": "approved", "expected_version": STUB_VERSION},
    )

    assert response.status_code == 422
    assert state["sent"] == []


@pytest.mark.asyncio
async def test_a_storage_failure_notifies_nobody(
    async_client: AsyncClient, review: Any
) -> None:
    """The approval was abandoned, so there is nothing to announce."""
    from app.services.storage_service import StorageError

    state = review(raises=StorageError("Copy failed with HTTP 404."))

    response = await async_client.patch(f"/api/v1/submissions/{SUBMISSION_ID}", json=_approve())

    assert response.status_code == 422
    assert state["sent"] == []


# ── delivery failure is not the reviewer's problem ───────────────────────────


@pytest.mark.asyncio
async def test_an_undelivered_message_does_not_change_the_response(
    async_client: AsyncClient, review: Any
) -> None:
    """The review is committed. Reporting failure now would invite a retry
    that hits the already-reviewed guard and looks like data loss."""
    state = review(plant=_plant(), delivered=False)

    response = await async_client.patch(f"/api/v1/submissions/{SUBMISSION_ID}", json=_approve())

    assert response.status_code == 200
    assert response.json()["data"]["plant_id"] == 42
    assert state["sent"], "it was still attempted"


@pytest.mark.asyncio
async def test_a_stale_version_notifies_nobody(
    async_client: AsyncClient, review: Any
) -> None:
    """Two features that landed separately, checked where they meet.

    The version guard and the notification were built on different branches
    and git merged them without a conflict. Nothing forced them to agree: a
    review refused for holding a stale view is a review that did not happen,
    so the contributor must not be told a decision was made. The guard raises
    before the endpoint reaches the notification, and this is what says so.
    """
    from app.services.submission_service import ContentChangedError

    state = review(raises=ContentChangedError("fedcba9876543210"))

    response = await async_client.patch(f"/api/v1/submissions/{SUBMISSION_ID}", json=_approve())

    assert response.status_code == 409
    assert "edited since you opened it" in response.json()["message"]
    assert state["sent"] == []
    assert "notify" not in state["events"]
