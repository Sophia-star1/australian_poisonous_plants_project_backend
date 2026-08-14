"""Plant submission endpoints (`UploadScreen` + `MySubmissionsScreen`).

Contributor:

* `POST   /submissions/`                      — multipart upload (photos + metadata).
* `GET    /submissions/me`                    — own submissions, newest first.
* `GET    /submissions/{id}`                  — own submission detail (admins: any).
* `PATCH  /submissions/{id}/content`          — edit own submission, pending only.
* `POST   /submissions/{id}/photos`           — add photos, pending only.
* `DELETE /submissions/{id}/photos/{photo_id}`— remove a photo, pending only.

Admin:

* `GET   /submissions/`      — list all with optional filters.
* `PATCH /submissions/{id}`  — approve or reject.

Once a submission has been approved or rejected it becomes read-only to its
contributor: the reviewer's decision refers to the content they saw, so
letting it change afterwards would leave an approved record nobody approved.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnprocessableError,
)
from app.db.session import get_db
from app.dependencies import get_current_active_user, get_optional_user, require_admin
from app.models.plant import Plant
from app.models.submission import Submission
from app.models.user import User
from app.schemas.common import ApiResponse, PageResult
from app.schemas.role import UserRole
from app.schemas.submission import (
    MAX_PHOTOS_PER_SUBMISSION,
    PENDING_STATUS,
    SubmissionContentUpdate,
    SubmissionCreate,
    SubmissionFilter,
    SubmissionOut,
    SubmissionReviewResult,
    SubmissionStatus,
    SubmissionUpdate,
)
from app.services.storage_service import StorageError
from app.services.submission_service import AlreadyReviewedError, SubmissionService

router = APIRouter()

#: Cap on photos accepted in a single submission.
MAX_PHOTOS_PER_SUBMISSION = 10


def _identification_warnings(plant: Plant | None) -> list[str]:
    """Flag a published entry that cannot answer the question users ask.

    Not an error — a reviewer may legitimately publish an identification and
    fill in the toxicology later. But this database is consulted during
    triage, and an entry that says nothing about toxicity reads as "not
    dangerous" to someone in a hurry. Saying so in the response is cheaper
    than discovering it in a clinical setting.
    """
    if plant is None:
        return []

    warnings: list[str] = []
    if plant.toxic_to_humans is None and not plant.toxic_to_animals:
        warnings.append(
            "No toxicity recorded: the entry does not state whether the plant is "
            "harmful to humans or animals."
        )
    if not plant.health_effects:
        warnings.append("No health effects recorded: symptom search will not find this plant.")
    if not plant.toxic_parts:
        warnings.append("No toxic parts recorded.")
    return warnings


def _validate_photos(photos: list[UploadFile]) -> None:
    """Reject anything that is not a plausible plant photo.

    Checked before a single byte is uploaded, so a bad request costs nothing
    in storage. The declared content type is not trustworthy on its own — it
    is client-supplied — but rejecting on it stops the honest mistakes, and
    the storage key never carries an executable extension either way.
    """
    if not photos:
        raise UnprocessableError("At least one photo is required.")
    if len(photos) > MAX_PHOTOS_PER_SUBMISSION:
        raise UnprocessableError(
            f"A submission may include at most {MAX_PHOTOS_PER_SUBMISSION} photos."
        )
    for photo in photos:
        content_type = (photo.content_type or "").split(";")[0].strip().lower()
        if content_type not in settings.ALLOWED_IMAGE_TYPES:
            allowed = ", ".join(settings.ALLOWED_IMAGE_TYPES)
            raise UnprocessableError(
                f"{photo.filename or 'file'}: unsupported type "
                f"'{content_type or 'unknown'}'. Allowed: {allowed}."
            )


def _is_owner(submission: Submission, user: User) -> bool:
    """Ownership is by contributor email — the schema has no user FK."""
    return submission.contributor_email.lower() == user.email.lower()


async def _load_for_user(
    service: SubmissionService, submission_id: UUID, user: User
) -> Submission:
    """Fetch a submission the caller is allowed to see.

    A stranger gets 404 rather than 403: answering 'forbidden' would confirm
    that the id exists, letting anyone probe for valid submission ids.
    """
    submission = await service.get_by_id(submission_id)
    if submission is None:
        raise NotFoundError(f"Submission {submission_id} not found.")
    if user.role != UserRole.ADMIN.value and not _is_owner(submission, user):
        raise NotFoundError(f"Submission {submission_id} not found.")
    return submission


def _require_editable(submission: Submission, user: User) -> None:
    """Reject edits to somebody else's, or to an already-reviewed, submission."""
    if not _is_owner(submission, user):
        # Admins can review but not rewrite a contributor's account of what
        # they saw; that would erase the distinction between the two roles.
        raise ForbiddenError("Only the contributor can edit a submission.")
    if submission.status != PENDING_STATUS:
        raise ConflictError(
            f"This submission has already been {submission.status} and can no longer be edited."
        )


@router.post("/", response_model=ApiResponse[SubmissionOut], status_code=201)
async def create_submission(
    photos: list[UploadFile] = File(..., description="Plant photos (jpg/png/webp, at least one)."),
    user_description: str = Form(""),
    contributor_name: str = Form(...),
    contributor_email: str = Form(...),
    location_state: str | None = Form(None),
    latitude: float | None = Form(None),
    longitude: float | None = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
) -> ApiResponse[SubmissionOut]:
    """Create a new observation from the mobile Upload tab.

    Upload one or more photos under the same `photos` field name.
    The first photo in the list is marked as the primary image.

    Anonymous submissions are still accepted, but when a bearer token is
    present the contributor identity is taken from it and the corresponding
    form fields are ignored. `GET /submissions/me` matches on
    `contributor_email`, so a client-supplied address was not merely a data
    quality problem: submitting under someone else's email placed the record
    in that person's list.

    Photos are stored in the `submission-images` bucket and stay there as an
    audit trail; they are copied into `plant-images` only if a reviewer
    approves the submission.
    """
    _validate_photos(photos)

    if current_user is not None:
        contributor_email = current_user.email
        contributor_name = current_user.full_name or current_user.email.split("@")[0]

    try:
        payload = SubmissionCreate(
            user_description=user_description or None,
            contributor_name=contributor_name,
            contributor_email=contributor_email,
            location_state=location_state,
            latitude=latitude,
            longitude=longitude,
        )
    except ValidationError as exc:
        raise UnprocessableError(str(exc)) from exc

    try:
        row = await SubmissionService(db).create(payload=payload, photos=photos)
    except StorageError as exc:
        # Surface the storage failure instead of writing a submission whose
        # photos do not exist. The session rolls back with the request.
        raise UnprocessableError(str(exc)) from exc
    return ApiResponse.success(data=SubmissionOut.model_validate(row))


@router.get("/me", response_model=ApiResponse[PageResult[SubmissionOut]])
async def list_my_submissions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResult[SubmissionOut]]:
    """Return submissions whose contributor_email matches the user."""
    rows, total = await SubmissionService(db).list_for_email(
        email=current_user.email, page=page, page_size=page_size
    )
    return ApiResponse.success(
        data=PageResult[SubmissionOut](
            items=[SubmissionOut.model_validate(r) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.get("/{submission_id}", response_model=ApiResponse[SubmissionOut])
async def get_submission(
    submission_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ApiResponse[SubmissionOut]:
    """Read one submission: the contributor's own, or any for an admin.

    This is the read-only view a contributor gets once their submission has
    been reviewed.
    """
    submission = await _load_for_user(SubmissionService(db), submission_id, current_user)
    return ApiResponse.success(data=SubmissionOut.model_validate(submission))


@router.patch("/{submission_id}/content", response_model=ApiResponse[SubmissionOut])
async def update_submission_content(
    submission_id: UUID,
    payload: SubmissionContentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ApiResponse[SubmissionOut]:
    """Edit your own submission while it is still awaiting review.

    Distinct from `PATCH /submissions/{id}`, which is the reviewer's
    approve/reject action — different actor, different permission, different
    payload, so they stay separate endpoints.
    """
    service = SubmissionService(db)
    submission = await _load_for_user(service, submission_id, current_user)
    _require_editable(submission, current_user)

    updated = await service.update_content(submission_id, payload)
    if updated is None:
        raise ConflictError(
            "This submission was reviewed while you were editing. Reload to see the outcome."
        )
    return ApiResponse.success(data=SubmissionOut.model_validate(updated))


@router.post(
    "/{submission_id}/photos",
    response_model=ApiResponse[SubmissionOut],
    status_code=201,
)
async def add_submission_photos(
    submission_id: UUID,
    photos: list[UploadFile] = File(..., description="Additional photos to attach."),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ApiResponse[SubmissionOut]:
    """Attach more photos to your own pending submission."""
    if not photos:
        raise UnprocessableError("At least one photo is required.")

    service = SubmissionService(db)
    submission = await _load_for_user(service, submission_id, current_user)
    _require_editable(submission, current_user)

    existing = await service.count_photos(submission_id)
    if existing + len(photos) > MAX_PHOTOS_PER_SUBMISSION:
        raise UnprocessableError(
            f"A submission may hold at most {MAX_PHOTOS_PER_SUBMISSION} photos "
            f"({existing} already attached)."
        )

    updated = await service.add_photos(submission_id, photos)
    if updated is None:
        raise ConflictError(
            "This submission was reviewed while you were editing. Reload to see the outcome."
        )
    return ApiResponse.success(data=SubmissionOut.model_validate(updated))


@router.delete(
    "/{submission_id}/photos/{photo_id}", response_model=ApiResponse[SubmissionOut]
)
async def delete_submission_photo(
    submission_id: UUID,
    photo_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ApiResponse[SubmissionOut]:
    """Remove one photo from your own pending submission.

    The last photo cannot be removed: a submission with no image is useless
    to a reviewer, and `POST /submissions/` requires one to begin with.
    """
    service = SubmissionService(db)
    submission = await _load_for_user(service, submission_id, current_user)
    _require_editable(submission, current_user)

    if await service.count_photos(submission_id) <= 1:
        raise UnprocessableError(
            "A submission must keep at least one photo. Add a replacement before "
            "removing this one."
        )

    updated = await service.delete_photo(submission_id, photo_id)
    if updated is None:
        raise NotFoundError(f"Photo {photo_id} not found on this submission.")
    return ApiResponse.success(data=SubmissionOut.model_validate(updated))


# ── Admin endpoints ──────────────────────────────────────────────────────────


@router.get("/", response_model=ApiResponse[PageResult[SubmissionOut]])
async def list_all_submissions(
    status: SubmissionStatus | None = Query(None, description="Filter: pending, approved, or rejected."),
    contributor_name: str | None = Query(None, description="Case-insensitive substring match."),
    contributor_email: str | None = Query(None, description="Case-insensitive substring match."),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> ApiResponse[PageResult[SubmissionOut]]:
    """Admin: list all submissions with optional filters."""
    filters = SubmissionFilter(
        status=status,
        contributor_name=contributor_name,
        contributor_email=contributor_email,
        page=page,
        page_size=page_size,
    )
    rows, total = await SubmissionService(db).list_all(filters)
    return ApiResponse.success(
        data=PageResult[SubmissionOut](
            items=[SubmissionOut.model_validate(r) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.patch("/{submission_id}", response_model=ApiResponse[SubmissionReviewResult])
async def update_submission_status(
    submission_id: UUID,
    payload: SubmissionUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ApiResponse[SubmissionReviewResult]:
    """Admin: approve or reject a submission.

    Approving publishes the observation: it creates a `plants` row from the
    reviewer's identification and copies the photos into `plant-images`. The
    originals stay in `submission-images` as the record of what was reviewed.

    All three steps share one transaction. A storage failure while copying
    aborts the approval rather than leaving a published plant with no
    photographs.
    """
    svc = SubmissionService(db)
    try:
        result = await svc.update_status(
            submission_id, payload=payload, reviewed_by=str(admin.email)
        )
    except AlreadyReviewedError as exc:
        raise ConflictError(str(exc)) from exc
    except StorageError as exc:
        raise UnprocessableError(
            f"Could not copy the photos into the plant library: {exc}"
        ) from exc

    if result is None:
        raise NotFoundError(f"Submission {submission_id} not found.")

    submission, plant = result
    warnings = _identification_warnings(plant)
    return ApiResponse.success(
        data=SubmissionReviewResult(
            submission=SubmissionOut.model_validate(submission),
            plant_id=plant.id if plant else None,
            warnings=warnings,
        ),
        message="Submission reviewed.",
    )
