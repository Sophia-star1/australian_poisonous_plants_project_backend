"""Plant submission endpoints (`UploadScreen` + `MySubmissionsScreen`).

* `POST /submissions/`       — multipart upload (photo + metadata).
* `GET  /submissions/me`     — current user's submissions, newest first.
* `GET  /submissions/`       — admin: list all with optional filters.
* `GET  /submissions/{id}`   — admin: single submission detail.
* `PATCH /submissions/{id}`  — admin: approve or reject.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, UnprocessableError
from app.db.session import get_db
from app.dependencies import get_current_active_user, get_optional_user, require_role
from app.models.user import User
from app.schemas.common import ApiResponse, PageResult
from app.schemas.role import UserRole
from app.schemas.submission import (
    SubmissionCreate,
    SubmissionFilter,
    SubmissionOut,
    SubmissionStatus,
    SubmissionUpdate,
)
from app.services.submission_service import SubmissionService

router = APIRouter()


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
    """
    if not photos:
        raise UnprocessableError("At least one photo is required.")

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

    row = await SubmissionService(db).create(payload=payload, photos=photos)
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


# ── Admin endpoints ──────────────────────────────────────────────────────────


@router.get("/", response_model=ApiResponse[PageResult[SubmissionOut]])
async def list_all_submissions(
    status: SubmissionStatus | None = Query(None, description="Filter: pending, approved, or rejected."),
    contributor_name: str | None = Query(None, description="Case-insensitive substring match."),
    contributor_email: str | None = Query(None, description="Case-insensitive substring match."),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_role(UserRole.ADMIN)),
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


@router.get("/{submission_id}", response_model=ApiResponse[SubmissionOut])
async def get_submission(
    submission_id: UUID,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_role(UserRole.ADMIN)),
) -> ApiResponse[SubmissionOut]:
    """Admin: get a single submission's full detail."""
    row = await SubmissionService(db).get_by_id(submission_id)
    if row is None:
        raise NotFoundError(f"Submission {submission_id} not found.")
    return ApiResponse.success(data=SubmissionOut.model_validate(row))


@router.patch("/{submission_id}", response_model=ApiResponse[SubmissionOut])
async def update_submission_status(
    submission_id: UUID,
    payload: SubmissionUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_role(UserRole.ADMIN)),
) -> ApiResponse[SubmissionOut]:
    """Admin: approve or reject a submission."""
    svc = SubmissionService(db)
    row = await svc.update_status(
        submission_id, payload=payload, reviewed_by=str(admin.email)
    )
    if row is None:
        raise NotFoundError(f"Submission {submission_id} not found.")
    return ApiResponse.success(data=SubmissionOut.model_validate(row))
