"""Admin configuration endpoints.

The reviewer UI needs to tune the AI screening without a deploy: how
confident the model must be for a photo to pass without a second look, and
below what confidence an upload is refused outright. Both numbers live in a
single database row (`screening_thresholds`); see
`app/services/screening_config_service.py` for how the running instances
converge on it.

* `GET   /admin/screening`  — the current thresholds.
* `PATCH /admin/screening`  — change one or both.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import UnprocessableError
from app.db.session import get_db
from app.dependencies import require_admin
from app.models.user import User
from app.schemas.common import ApiResponse
from app.schemas.screening import ScreeningConfigOut, ScreeningConfigUpdate
from app.services.screening_config_service import ScreeningConfigService

router = APIRouter()


@router.get("/screening", response_model=ApiResponse[ScreeningConfigOut])
async def get_screening_config(
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> ApiResponse[ScreeningConfigOut]:
    """The thresholds the classifier grades against. Admin.

    `source` is `"database"` once an admin has changed them, `"defaults"`
    otherwise — the configured `PLANT_REJECT_BELOW` / `PLANT_ACCEPT_AT`.

    Errors:

    * `403` — not an administrator.
    """
    service = ScreeningConfigService(db)
    row = await service.get()
    if row is None:
        out = ScreeningConfigOut(
            reject_below=settings.PLANT_REJECT_BELOW,
            accept_at=settings.PLANT_ACCEPT_AT,
            source="defaults",
        )
    else:
        out = ScreeningConfigOut(
            reject_below=row.reject_below,
            accept_at=row.accept_at,
            source="database",
            updated_at=row.updated_at,
            updated_by=row.updated_by,
        )
    return ApiResponse.success(data=out)


@router.patch("/screening", response_model=ApiResponse[ScreeningConfigOut])
async def update_screening_config(
    payload: ScreeningConfigUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ApiResponse[ScreeningConfigOut]:
    """Change the acceptance and refusal thresholds. Admin.

    Send only the fields to change:

    * `reject_below` — a photo scoring below this is refused outright;
    * `accept_at` — at or above this a photo passes cleanly; between the two
      it is accepted but flagged for the reviewer.

    The merged pair must satisfy `0 <= reject_below < accept_at <= 1`.
    Takes effect on the instance serving this request at once and on every
    other instance within about 30 seconds; it survives redeploys.

    Errors:

    * `403` — not an administrator.
    * `422` — a threshold outside `[0, 1]`, an unknown field, or a merged
      pair where `reject_below` is not below `accept_at`.
    """
    service = ScreeningConfigService(db)
    current = await service.get()

    reject_below = payload.reject_below
    if reject_below is None:
        reject_below = current.reject_below if current else settings.PLANT_REJECT_BELOW
    accept_at = payload.accept_at
    if accept_at is None:
        accept_at = current.accept_at if current else settings.PLANT_ACCEPT_AT

    if not 0.0 <= reject_below < accept_at <= 1.0:
        raise UnprocessableError(
            f"Thresholds must satisfy 0 <= reject_below ({reject_below}) "
            f"< accept_at ({accept_at}) <= 1."
        )

    row = await service.update(reject_below, accept_at, updated_by=str(admin.email))
    return ApiResponse.success(
        data=ScreeningConfigOut(
            reject_below=row.reject_below,
            accept_at=row.accept_at,
            source="database",
            updated_at=row.updated_at,
            updated_by=row.updated_by,
        ),
        message="Screening thresholds updated.",
    )
