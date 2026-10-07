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

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, UploadFile
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
from app.ml.classifier import ImageTooLargeError, UndecodableImageError
from app.models.plant import Plant, not_recorded
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
from app.services.notification_service import ReviewOutcome, notify_review
from app.services.storage_service import StorageError
from app.services.submission_service import (
    AlreadyReviewedError,
    ContentChangedError,
    NotAPlantError,
    SubmissionService,
)

router = APIRouter()


def _identification_warnings(plant: Plant | None) -> list[str]:
    """Flag a published entry that cannot answer the question users ask.

    Not an error — a reviewer may legitimately publish an identification and
    fill in the toxicology later. But this database is consulted during
    triage, and an entry that says nothing about toxicity reads as "not
    dangerous" to someone in a hurry. Saying so in the response is cheaper
    than discovering it in a clinical setting.

    Each check goes through `not_recorded` rather than testing for falsiness.
    In `plants_v2` a gap is the string `'Unknown'`, never NULL or an empty
    list, so `if not plant.health_effects` is False for every unrecorded entry
    and these warnings would never fire again.
    """
    if plant is None:
        return []

    warnings: list[str] = []
    if not_recorded(plant.toxicity_by_species):
        warnings.append(
            "No toxicity recorded: the entry does not state whether the plant is "
            "harmful to humans or animals."
        )
    if not_recorded(plant.health_effects):
        warnings.append("No health effects recorded: symptom search will not find this plant.")
    if not_recorded(plant.toxic_parts):
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


async def _commit_uploads(db: AsyncSession, service: SubmissionService) -> None:
    """Commit before answering, and take the uploaded photos back if that fails.

    `get_db` commits only after the response has been sent — measured on the
    deployed FastAPI 0.136 as well as 0.142 — so a commit failing there
    reaches nobody: the contributor has their 201, the rows are gone, and the
    photos sit in the bucket with nothing pointing at them. Committing here
    turns that into an error the client sees. The commit `get_db` makes
    afterwards finds nothing pending.
    """
    try:
        await db.commit()
    except Exception:
        await service.discard_uploaded_photos()
        raise


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
    """Create a new observation.

    Send one or more photographs under the `photos` field; the first is the
    primary image. Accepted types are JPEG, PNG and WebP, up to ten per
    submission.

    Signing in is optional but recommended: with a bearer token the
    contributor identity comes from the token and `contributor_name` /
    `contributor_email` in the form are ignored, and the submission then
    appears in `GET /submissions/me` and can be edited while it is pending.

    Every photograph is screened for whether it shows a plant, and every one
    of them must: if any photo clearly does not, the whole submission is
    refused with a message naming that photo by position. An uncertain photo
    is accepted and marked for the reviewer. The verdict is returned in
    `ai_is_plant` and `ai_top_matches`, and is `null` when screening was
    unavailable.

    Photographs are kept as the record of what was submitted, and copied into
    the plant library only if a reviewer approves.

    Errors:

    * `422` — unsupported file type, more than ten photographs, a file that is
      not a readable image, one that exceeds the size limit, or a photo that
      does not appear to be a plant.
    """
    _validate_photos(photos)

    if current_user is not None:
        # `GET /submissions/me` matches on contributor_email, so trusting a
        # client-supplied address was not merely a data quality problem:
        # submitting under someone else's email put the record in their list.
        contributor_email = current_user.email
        # Stripped first: a stored name of only spaces is truthy, and would
        # then fail the name's length check and refuse a signed-in upload.
        mailbox = current_user.email.split("@")[0]
        contributor_name = (current_user.full_name or "").strip() or mailbox

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

    service = SubmissionService(db)
    try:
        row = await service.create(payload=payload, photos=photos)
    except NotAPlantError as exc:
        # Screening is confident at least one photo does not show a plant.
        # Said plainly, naming the photo, so the contributor can fix it and
        # retry rather than face a generic rejection they cannot act on.
        raise UnprocessableError(str(exc)) from exc
    except ImageTooLargeError as exc:
        # The header declares more pixels than screening can decode safely.
        # A 17 KB file can claim 144 megapixels; decoding it is the attack.
        raise UnprocessableError(
            f"One of the photos is too large to process ({exc}). Export it at a "
            "smaller size, or as a JPEG, and try again."
        ) from exc
    except UndecodableImageError as exc:
        # The declared content type said image; the bytes disagree. Caught
        # here only because screening is the first thing that tries to decode
        # them — worth refusing either way, since a reviewer cannot look at it.
        raise UnprocessableError(
            "One of the files is not a readable image. Re-export it and try again."
        ) from exc
    except StorageError as exc:
        # Surface the storage failure instead of writing a submission whose
        # photos do not exist. The session rolls back with the request, and
        # the service has already deleted whatever it uploaded first.
        raise UnprocessableError(str(exc)) from exc

    out = SubmissionOut.model_validate(row)
    await _commit_uploads(db, service)
    return ApiResponse.success(data=out)


@router.get("/me", response_model=ApiResponse[PageResult[SubmissionOut]])
async def list_my_submissions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ApiResponse[PageResult[SubmissionOut]]:
    """Your own submissions, newest first.

    Matched on the signed-in account's address, so anything submitted
    anonymously under that address appears here too. `page` starts at 1 and
    `page_size` is capped at 100.
    """
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
    """One submission — your own, or any of them for an administrator.

    Includes `content_version`, which a reviewer must send back as
    `expected_version` when approving or rejecting.

    Works whatever the status: this is the read-only view a contributor keeps
    once their submission has been decided.

    Errors:

    * `404` — no such submission, or it belongs to somebody else. Both answer
      the same way so the endpoint cannot be used to discover which ids exist.
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

    Send only the fields you are changing: `contributor_name`,
    `user_description`, `location_state`, `latitude`, `longitude`. Anything
    omitted is left as it was. The status and the contributor's address are
    not editable here.

    Once reviewed a submission is frozen — the reviewer's decision refers to
    the content they saw. The read endpoint keeps working.

    Not to be confused with `PATCH /submissions/{id}`, which is the reviewer's
    approve/reject action.

    Errors:

    * `403` — an administrator editing somebody else's submission.
    * `404` — no such submission, or it is not yours. Both answer the same way
      so the endpoint cannot be used to discover which ids exist.
    * `409` — already approved or rejected.
    * `422` — an unknown field in the body.
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
    """Attach more photographs to your own pending submission.

    Same rules as the original upload: JPEG, PNG or WebP, and ten photographs
    per submission in total. Returns the whole submission, including a new
    `content_version` — any review in progress against the old one will now be
    refused, which is the point.

    Errors:

    * `403` — an administrator adding to somebody else's submission.
    * `404` — no such submission, or it is not yours.
    * `409` — already approved or rejected.
    * `422` — unsupported type, a file over the size limit, or the total would
      exceed ten.
    """
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

    try:
        updated = await service.add_photos(submission_id, photos)
    except StorageError as exc:
        # An oversized or empty file, or storage being unavailable. Caught on
        # upload but not here, so the same mistake was a 422 when creating a
        # submission and a 500 when adding to one.
        raise UnprocessableError(str(exc)) from exc
    if updated is None:
        raise ConflictError(
            "This submission was reviewed while you were editing. Reload to see the outcome."
        )
    out = SubmissionOut.model_validate(updated)
    await _commit_uploads(db, service)
    return ApiResponse.success(data=out)


@router.delete(
    "/{submission_id}/photos/{photo_id}", response_model=ApiResponse[SubmissionOut]
)
async def delete_submission_photo(
    submission_id: UUID,
    photo_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ApiResponse[SubmissionOut]:
    """Remove one photograph from your own pending submission.

    The last one cannot be removed — a submission with no image is no use to a
    reviewer, and creating one requires a photograph to begin with. Add the
    replacement first.

    If the removed photograph was the primary image another takes its place.
    Returns the whole submission with a new `content_version`.

    Errors:

    * `403` — an administrator removing from somebody else's submission.
    * `404` — no such submission or photograph, or it is not yours.
    * `409` — already approved or rejected.
    * `422` — it is the only photograph left.
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
    """Admin: one page of submissions, newest first.

    Filter by `status` (pending, approved or rejected), or by a
    case-insensitive fragment of `contributor_name` or `contributor_email`.
    `page` starts at 1 and `page_size` is capped at 100; the response carries
    `total`.

    Each item includes `content_version`, which is what
    `PATCH /submissions/{id}` requires as `expected_version`.
    """
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
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ApiResponse[SubmissionReviewResult]:
    """Admin: approve or reject a submission.

    Approving publishes the observation. It creates a plant entry from the
    identification supplied here and copies the photos into the plant library;
    the originals stay where they are as the record of what was reviewed.
    Rejecting records the decision and changes nothing else. Either way the
    contributor is emailed the outcome, and the submission becomes read-only.

    **`expected_version`** is the `content_version` of the submission you are
    looking at, from `GET /submissions/{id}`. A contributor may keep editing
    while a submission is pending, so if the description, the location or the
    photographs have changed since you loaded it the review is refused rather
    than applied — reload, see what changed, and decide again.

    **Approving** requires `plant` with at least `botanical_name`. Everything
    else about the plant is optional: the response returns `warnings` listing
    what the published entry does not say, rather than blocking on it. Fields
    you omit are left unset rather than blanked.

    **Rejecting** must not carry `plant`. `reviewer_notes` is passed on to the
    contributor in the email, so it is the most useful thing to fill in.

    Errors:

    * `409` — the submission was edited after you loaded it, or another
      reviewer decided first.
    * `422` — approving without an identification, rejecting with one, or a
      photograph that could not be copied into the library.
    """
    svc = SubmissionService(db)
    try:
        result = await svc.update_status(
            submission_id, payload=payload, reviewed_by=str(admin.email)
        )
    except ContentChangedError as exc:
        # The reviewer is holding a stale view. 409 rather than 422: nothing
        # about the request is malformed, it just no longer applies.
        raise ConflictError(str(exc)) from exc
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

    # Snapshot while the session is open. The task holds plain values, never
    # the row: reading a mapped attribute on a detached instance raises rather
    # than loading, which is what made every approval return 500 once already.
    outcome = ReviewOutcome.from_review(submission, plant)

    # Commit before scheduling, and not redundantly: background tasks run
    # *before* `get_db` reaches its own commit — measured on FastAPI 0.141 and
    # Starlette 1.6, against a real server as well as the test transport.
    # Scheduling without committing first leaves a window where the mail goes
    # out and the transaction then fails, telling a contributor their
    # submission was approved when nothing was written. Committing here closes
    # it; the commit `get_db` performs on the way out finds nothing pending.
    #
    # If this raises, no message is scheduled and the review rolls back, which
    # is the right outcome: nothing happened, so nobody is told anything. The
    # photos already copied into the plant bucket do not roll back with it, so
    # they are removed by hand.
    try:
        await db.commit()
    except Exception:
        await svc.discard_promoted_photos()
        raise
    background.add_task(notify_review, outcome)

    return ApiResponse.success(
        data=SubmissionReviewResult(
            submission=SubmissionOut.model_validate(submission),
            plant_id=plant.id if plant else None,
            warnings=warnings,
        ),
        message="Submission reviewed.",
    )
