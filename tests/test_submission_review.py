"""Tests for the approval flow: submission → plant entry + promoted photos.

Approval is the only path that writes to `plants`, so these cases pin what a
reviewer must supply and what happens when part of the work fails.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.exc import MissingGreenlet

from app.api.v1 import submissions as api
from app.db.session import get_db
from app.dependencies import get_current_active_user
from app.main import app
from app.models.plant import Plant, PlantImage
from app.models.submission import Submission, SubmissionImage
from app.models.user import User
from app.schemas.submission import (
    REVIEWER_NOTES_MAX_LENGTH,
    PlantIdentification,
    SubmissionUpdate,
)
from app.services.storage_service import StorageError, StoredFile
from app.services.submission_service import (
    AlreadyReviewedError,
    SubmissionService,
    _unknown_defaults,
    submission_fingerprint,
)

SUBMISSION_ID = UUID("33333333-3333-3333-3333-333333333333")

#: Cache of the stand-in classes built by `_expire`, keyed by the real model.
_EXPIRED_CLASSES: dict[type, type] = {}


def _submission(status: str = "pending", images: int = 2) -> Submission:
    """Build a detached Submission with photos already 'uploaded'."""
    sub = Submission()
    sub.id = SUBMISSION_ID
    sub.contributor_name = "Ada"
    sub.contributor_email = "ada@example.com"
    sub.user_description = "beside the creek"
    sub.location_state = "NSW"
    sub.latitude = None
    sub.longitude = None
    sub.status = status
    sub.ai_is_plant = None
    sub.ai_top_matches = None
    sub.reviewer_notes = None
    sub.reviewed_by = None
    sub.reviewed_at = None
    sub.submitted_at = datetime.now(UTC)
    sub.images = [_photo(i, i == 0) for i in range(images)]
    return sub


def _photo(index: int, is_primary: bool) -> SubmissionImage:
    """One already-uploaded submission photo."""
    image = SubmissionImage()
    image.id = uuid4()
    image.submission_id = SUBMISSION_ID
    image.image_url = (
        f"https://proj.supabase.co/storage/v1/object/public/"
        f"submission-images/submissions/{SUBMISSION_ID}/photo{index}.jpg"
    )
    image.is_primary = is_primary
    return image


def _expire(instance: Any) -> type:
    """Make every column read on `instance` fail, as an expired one does.

    SQLAlchemy's `expire_all()` marks loaded attributes stale; the next read
    re-queries the database. Under asyncio that read happens outside the
    greenlet and raises `MissingGreenlet` instead of fetching. Returns the
    original class so a later `get()` can undo it, which is what a real
    reload does — the identity map hands back the same object, refreshed.
    """
    original = type(instance)
    if original not in _EXPIRED_CLASSES:

        def guard(self: Any, name: str) -> Any:
            if name.startswith("__") or name == "_sa_instance_state":
                return object.__getattribute__(self, name)
            raise MissingGreenlet(
                f"greenlet_spawn has not been called; reading {name!r} on an "
                "expired instance would go to the database"
            )

        # Built once per model: each subclass of a declarative base registers
        # itself, and rebuilding it per call floods the run with SAWarnings.
        _EXPIRED_CLASSES[original] = type(
            f"Expired{original.__name__}", (original,), {"__getattribute__": guard}
        )
    instance.__class__ = _EXPIRED_CLASSES[original]
    return original


class FakeSession:
    """Records what the service writes, without a database.

    `expire_all` is modelled rather than stubbed out. The no-op version let
    approval pass every test while returning 500 in production: the service
    expired the Plant it had just created and handed that instance back, so
    the endpoint's first read of `plant.id` tried to load from a closed
    async context.
    """

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.guard_matches = True
        self.reloaded: list[Any] = []
        self._expired: list[tuple[Any, type, Any]] = []
        #: Raised by the first flush after a PlantImage is added — where the
        #: one-primary index or the `(plant_id, image_url)` key would fire.
        self.image_flush_error: Exception | None = None
        self.commit_error: Exception | None = None

    async def get(self, model: type, primary_key: Any) -> Any:
        """Undo expiry for the matching instance, as a real reload would."""
        for instance, original, saved_pk in self._expired:
            if isinstance(instance, model) and saved_pk == primary_key:
                instance.__class__ = original
                self.reloaded.append(primary_key)
                return instance
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        if isinstance(obj, Plant) and obj.id is None:
            obj.id = 42

    async def execute(self, statement: Any) -> Any:
        class _Result:
            def __init__(self, matched: bool) -> None:
                self._matched = matched

            def first(self) -> tuple[Any, ...] | None:
                return (SUBMISSION_ID,) if self._matched else None

        return _Result(self.guard_matches)

    async def flush(self) -> None:
        if self.image_flush_error and any(isinstance(o, PlantImage) for o in self.added):
            raise self.image_flush_error
        for obj in self.added:
            if isinstance(obj, Plant) and obj.id is None:
                obj.id = 42

    def expire_all(self) -> None:
        for obj in self.added:
            if isinstance(obj, Plant):
                primary_key = obj.id  # read before expiring, or this raises
                self._expired.append((obj, _expire(obj), primary_key))

    async def refresh(self, obj: Any, attribute_names: list[str] | None = None) -> None:
        return None

    async def commit(self) -> None:
        if self.commit_error:
            raise self.commit_error

    async def rollback(self) -> None:
        return None


@pytest.fixture
def review(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Wire the endpoint to fakes and expose what was written."""
    state: dict[str, Any] = {"copies": [], "deletes": [], "session": FakeSession()}

    def _apply(
        *,
        status: str = "pending",
        guard: bool = True,
        copy_fails: bool = False,
        copy_fails_at: int | None = None,
        images: int = 2,
    ) -> dict[str, Any]:
        row = _submission(status=status, images=images)
        state["submission"] = row
        state["session"].guard_matches = guard

        async def _get_by_id(self: Any, submission_id: UUID) -> Submission | None:
            return row

        async def _copy_object(
            self: Any,
            source_key: str,
            destination_key: str,
            source_bucket: str,
            destination_bucket: str,
        ) -> StoredFile:
            if copy_fails or len(state["copies"]) == copy_fails_at:
                raise StorageError("Copy failed with HTTP 404.")
            state["copies"].append(
                {
                    "source": f"{source_bucket}/{source_key}",
                    "destination": f"{destination_bucket}/{destination_key}",
                }
            )
            return StoredFile(
                key=destination_key,
                url=f"https://proj.supabase.co/storage/v1/object/public/"
                f"{destination_bucket}/{destination_key}",
            )

        async def _delete_file(self: Any, key: str, bucket: str | None = None) -> bool:
            state["deletes"].append(f"{bucket}/{key}")
            return True

        monkeypatch.setattr(SubmissionService, "get_by_id", _get_by_id)
        monkeypatch.setattr(
            "app.services.submission_service.StorageService.copy_object", _copy_object
        )
        monkeypatch.setattr(
            "app.services.submission_service.StorageService.delete_file", _delete_file
        )

        async def _fake_db() -> AsyncIterator[FakeSession]:
            yield state["session"]

        def _admin() -> User:
            user = User()
            user.id = 1
            user.email = "reviewer@example.com"
            user.full_name = "Reviewer"
            user.is_active = True
            user.role = "admin"
            return user

        app.dependency_overrides[get_db] = _fake_db
        # Override the user lookup rather than the role check itself:
        # require_role builds a fresh closure per call so it cannot be keyed
        # on, and this way the real permission logic still runs.
        app.dependency_overrides[get_current_active_user] = _admin
        return state

    yield _apply
    app.dependency_overrides.clear()


def _review(state: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """A review body carrying the version the reviewer would have been shown."""
    body: dict[str, Any] = {
        "status": "approved",
        "expected_version": submission_fingerprint(state["submission"]),
    }
    body.update(overrides)
    return body


def _identification(**overrides: Any) -> dict[str, Any]:
    base = {"botanical_name": "Nerium oleander"}
    base.update(overrides)
    return base


# ── schema rules ─────────────────────────────────────────────────────────────


def test_approval_requires_an_identification() -> None:
    """A submission has no botanical_name, so approval cannot infer one."""
    with pytest.raises(ValidationError, match="requires 'plant'"):
        SubmissionUpdate(status="approved", expected_version="v1")


def test_rejection_must_not_carry_an_identification() -> None:
    """Rejecting and publishing at once is incoherent."""
    with pytest.raises(ValidationError, match="only meaningful when approving"):
        SubmissionUpdate(
            status="rejected",
            expected_version="v1",
            plant=PlantIdentification(botanical_name="X"),
        )


def test_identification_rejects_unknown_fields() -> None:
    """extra='forbid' stops a typo silently dropping toxicity data."""
    with pytest.raises(ValidationError):
        PlantIdentification(botanical_name="X", toxic_to_human=True)


def test_botanical_name_is_mandatory() -> None:
    """It is the only NOT NULL column on plants."""
    with pytest.raises(ValidationError):
        PlantIdentification(botanical_name="")


def test_reviewer_notes_are_bounded() -> None:
    """They are quoted in full in the contributor's email."""
    limit = REVIEWER_NOTES_MAX_LENGTH
    SubmissionUpdate(status="rejected", expected_version="v1", reviewer_notes="x" * limit)
    with pytest.raises(ValidationError, match="at most 2000 characters"):
        SubmissionUpdate(status="rejected", expected_version="v1", reviewer_notes="x" * (limit + 1))


# ── approval ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approval_creates_a_plant_and_promotes_photos(
    async_client: AsyncClient, review: Any
) -> None:
    """The whole chain in one request."""
    state = review()
    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(
            state,
            plant=_identification(
                toxicity_by_species={"humans": "Toxic"},
                toxic_parts=["Leaves", "Seeds"],
                health_effects="Vomiting, diarrhoea, abdominal pain.",
            ),
        ),
    )
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["plant_id"] == 42

    plants = [o for o in state["session"].added if isinstance(o, Plant)]
    assert len(plants) == 1
    assert plants[0].botanical_name == "Nerium oleander"

    assert len(state["copies"]) == 2
    for copy in state["copies"]:
        assert copy["source"].startswith("submission-images/")
        assert copy["destination"].startswith("plant-images-v2/plants/42/")


@pytest.mark.asyncio
async def test_promoted_images_keep_one_primary(
    async_client: AsyncClient, review: Any
) -> None:
    """The lead photo stays the lead photo in the plant library."""
    state = review()
    await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(state, plant=_identification()),
    )
    images = [o for o in state["session"].added if type(o).__name__ == "PlantImage"]
    assert len(images) == 2
    assert sum(1 for i in images if i.is_primary) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("flags", [(True, True, False), (False, False, False)])
async def test_promotion_yields_one_primary_whatever_the_submission_says(
    async_client: AsyncClient, review: Any, flags: tuple[bool, ...]
) -> None:
    """Two primaries, or none, still publish exactly one cover.

    Nothing constrains `submission_images`, and concurrent `add_photos` calls
    on a submission with no photos left each mark theirs primary. Carried
    across as-is, two flags violate `plant_images_v2_one_primary_per_plant`
    and the approval dies with a 500 after the copies were made; no flag
    publishes a plant without a cover.
    """
    state = review()
    row = state["submission"]
    row.images = [_photo(i, primary) for i, primary in enumerate(flags)]
    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(state, plant=_identification()),
    )
    assert response.status_code == 200, response.text

    images = [o for o in state["session"].added if type(o).__name__ == "PlantImage"]
    assert len(images) == len(flags)
    assert sum(1 for i in images if i.is_primary) == 1
    # The caption follows the photo that ends up primary, not the stale flag.
    assert [i.caption is not None for i in images] == [i.is_primary for i in images]


@pytest.mark.asyncio
async def test_only_the_primary_photo_gets_the_contributors_caption(
    async_client: AsyncClient, review: Any
) -> None:
    """The description belongs to the submission, not to each photograph.

    Copying it onto every promoted photo made a multi-photo approval render the
    same sentence under three different pictures, which reads as a bug in the
    gallery rather than as one description. Uncovered until now — the caption
    was not asserted anywhere, so changing it broke no test.
    """
    state = review()
    await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(state, plant=_identification()),
    )
    images = [o for o in state["session"].added if type(o).__name__ == "PlantImage"]

    primary = [i for i in images if i.is_primary]
    rest = [i for i in images if not i.is_primary]
    assert len(primary) == 1
    assert primary[0].caption is not None
    assert all(i.caption is None for i in rest)


@pytest.mark.asyncio
async def test_observed_location_is_carried_into_the_plant(
    async_client: AsyncClient, review: Any
) -> None:
    """The one botanical fact a submission genuinely carries.

    v1 put this in `distribution_observed`, which kept it distinguishable from
    a formal listing. v2 has one merged `distribution`, so a contributor's
    sighting now sits alongside curated distribution data.
    """
    state = review()
    await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(state, plant=_identification()),
    )
    plant = next(o for o in state["session"].added if isinstance(o, Plant))
    assert plant.distribution == ["NSW"]


@pytest.mark.asyncio
async def test_reviewer_supplied_distribution_wins(
    async_client: AsyncClient, review: Any
) -> None:
    """The fallback must not override an explicit answer."""
    state = review()
    await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(state, plant=_identification(distribution=["QLD", "NT"])),
    )
    plant = next(o for o in state["session"].added if isinstance(o, Plant))
    assert plant.distribution == ["QLD", "NT"]


@pytest.mark.asyncio
async def test_partial_objects_are_completed_with_unknown(
    async_client: AsyncClient, review: Any
) -> None:
    """A one-key object would sit beside 100 rows that carry every key.

    The reviewer's values win, the rest are the dataset's 'Unknown', and an
    extra morphology key the reviewer added is kept.
    """
    state = review()
    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(
            state,
            plant=_identification(
                toxicity_by_species={"humans": "toxic", "notes": "seeds"},
                toxin_profile={"primary_class": "Cardiac glycosides"},
                morphological_attributes={"height_m": 3, "rhizome": "thick"},
            ),
        ),
    )
    assert response.status_code == 200, response.text
    plant = next(o for o in state["session"].added if isinstance(o, Plant))

    species = plant.toxicity_by_species
    assert set(species) == set(_unknown_defaults()["toxicity_by_species"])
    assert species["humans"] == "Toxic"
    assert species["notes"] == "seeds"
    assert {species[k] for k in species if k not in ("humans", "notes")} == {"Unknown"}
    assert plant.toxic_to_humans is True

    assert plant.toxin_profile == {
        "primary_class": "Cardiac glycosides",
        "secondary_class": "Unknown",
        "toxins_implicated": "Unknown",
    }

    morph = plant.morphological_attributes
    assert set(_unknown_defaults()["morphological_attributes"]) <= set(morph)
    assert morph["height_m"] == 3
    assert morph["rhizome"] == "thick"
    assert morph["woodiness"] == "Unknown"


@pytest.mark.asyncio
async def test_missing_toxicity_data_is_flagged(
    async_client: AsyncClient, review: Any
) -> None:
    """Publishing silence about toxicity should not be quiet."""
    state = review()
    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(state, plant=_identification()),
    )
    warnings = response.json()["data"]["warnings"]
    assert any("No toxicity recorded" in w for w in warnings)
    assert any("health effects" in w for w in warnings)


@pytest.mark.asyncio
async def test_complete_identification_produces_no_warnings(
    async_client: AsyncClient, review: Any
) -> None:
    """A fully filled entry is not nagged."""
    state = review()
    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(
            state,
            plant=_identification(
                toxicity_by_species={"humans": "Toxic"},
                toxic_parts=["Leaves"],
                health_effects="Vomiting, diarrhoea, abdominal pain.",
            ),
        ),
    )
    assert response.json()["data"]["warnings"] == []


# ── failure paths ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_copy_failure_aborts_the_approval(
    async_client: AsyncClient, review: Any
) -> None:
    """A published plant with no photographs is worse than a failed review."""
    state = review(copy_fails=True)
    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(state, plant=_identification()),
    )
    assert response.status_code == 422
    assert "Could not copy the photos" in response.json()["message"]


def _copied(state: dict[str, Any]) -> list[str]:
    return [c["destination"] for c in state["copies"]]


@pytest.mark.asyncio
async def test_a_copy_failing_midway_removes_the_photos_already_copied(
    async_client: AsyncClient, review: Any
) -> None:
    """The rollback takes the rows; the objects have to be taken by hand.

    Third of three photos fails: the first two are in the plant bucket, and
    with the transaction gone nothing would ever reference or delete them.
    """
    state = review(images=3, copy_fails_at=2)
    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(state, plant=_identification()),
    )
    assert response.status_code == 422
    assert len(state["copies"]) == 2
    assert sorted(state["deletes"]) == sorted(_copied(state))


@pytest.mark.asyncio
async def test_a_failed_flush_removes_the_copied_photos(
    async_client: AsyncClient, review: Any
) -> None:
    """E.g. the one-primary index or the `(plant_id, image_url)` key firing."""
    state = review()
    state["session"].image_flush_error = RuntimeError("duplicate key value")
    with pytest.raises(RuntimeError, match="duplicate key"):
        await async_client.patch(
            f"/api/v1/submissions/{SUBMISSION_ID}",
            json=_review(state, plant=_identification()),
        )
    assert len(state["copies"]) == 2
    assert sorted(state["deletes"]) == sorted(_copied(state))


@pytest.mark.asyncio
async def test_a_failed_commit_removes_the_copied_photos(
    async_client: AsyncClient, review: Any
) -> None:
    """The endpoint owns the commit, so the endpoint has to clean up after it."""
    state = review()
    state["session"].commit_error = RuntimeError("connection was closed")
    with pytest.raises(RuntimeError, match="connection was closed"):
        await async_client.patch(
            f"/api/v1/submissions/{SUBMISSION_ID}",
            json=_review(state, plant=_identification()),
        )
    assert len(state["copies"]) == 2
    assert sorted(state["deletes"]) == sorted(_copied(state))


@pytest.mark.asyncio
async def test_a_successful_approval_deletes_nothing(
    async_client: AsyncClient, review: Any
) -> None:
    """The cleanup must not fire on the path where the photos are wanted."""
    state = review()
    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(state, plant=_identification()),
    )
    assert response.status_code == 200
    assert state["deletes"] == []


@pytest.mark.asyncio
async def test_second_reviewer_gets_409(async_client: AsyncClient, review: Any) -> None:
    """The guarded UPDATE matched nothing: someone decided first."""
    state = review(guard=False)
    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(state, plant=_identification()),
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_rejection_creates_no_plant(async_client: AsyncClient, review: Any) -> None:
    """Rejecting leaves the plant library untouched."""
    state = review()
    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(state, status="rejected", reviewer_notes="blurry photo"),
    )
    assert response.status_code == 200
    assert response.json()["data"]["plant_id"] is None
    assert [o for o in state["session"].added if isinstance(o, Plant)] == []
    assert state["copies"] == []


def test_already_reviewed_error_reports_the_current_status() -> None:
    """The message tells the second reviewer what happened, not just that it did."""
    error = AlreadyReviewedError("approved")
    assert "already been approved" in str(error)
    assert error.current_status == "approved"


def test_warnings_helper_is_silent_for_a_rejection() -> None:
    """No plant, nothing to warn about."""
    assert api._identification_warnings(None) == []


@pytest.mark.asyncio
async def test_approved_plant_survives_the_identity_map_being_expired(
    async_client: AsyncClient, review: Any
) -> None:
    """Regression: approving returned 500 for every submission.

    `update_status` ends with `expire_all()`, which invalidates the Plant it
    had just created, and then returned that same instance. The endpoint
    reads `plant.id` and its toxicity fields to build the response, so the
    first attribute access attempted a lazy load in an async context and
    raised MissingGreenlet — surfacing as a bare 500 with the submission
    already marked approved and the photos already copied.

    Every existing test passed because the fake session made `expire_all` a
    no-op. It is modelled now, so this asserts the service hands back an
    instance the caller can actually read.
    """
    state = review()

    response = await async_client.patch(
        f"/api/v1/submissions/{SUBMISSION_ID}",
        json=_review(state, plant=_identification()),
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["plant_id"] == 42
    assert state["session"].reloaded == [42], "the plant was not re-loaded after expiry"
