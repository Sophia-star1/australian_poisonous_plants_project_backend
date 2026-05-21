"""Plant submission endpoints (`UploadScreen` + `MySubmissionsScreen`).

* `POST /submissions/`   — multipart upload (photo + metadata).
* `GET  /submissions/me` — current user's submissions, newest first.
"""

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UnprocessableError
from app.db.session import get_db
from app.dependencies import get_current_active_user
from app.models.user import User
from app.schemas.common import ApiResponse, PageResult
from app.schemas.submission import SubmissionCreate, SubmissionOut
from app.services.submission_service import SubmissionService

router = APIRouter()


@router.post("/", response_model=ApiResponse[SubmissionOut], status_code=201)
async def create_submission(
    photo: UploadFile = File(..., description="Plant photo (jpg/png/webp)."),
    user_description: str = Form(""),
    contributor_name: str = Form(...),
    contributor_email: str = Form(...),
    location_state: str | None = Form(None),
    latitude: float | None = Form(None),
    longitude: float | None = Form(None),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[SubmissionOut]:
    """Create a new observation from the mobile Upload tab."""
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

    row = await SubmissionService(db).create(payload=payload, photo=photo)
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
