"""Badge endpoints.

* `GET /badges/levels`          — public: the tree stages and reviewer tiers.
* `GET /badges/me`              — the signed-in account's tree and badges.
* `GET /badges/users/{user_id}` — admin: any account's.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import NotFoundError
from app.db.session import get_db
from app.dependencies import get_current_active_user, require_admin
from app.models.user import User
from app.schemas.badge import BadgeLevels, BadgeSummary
from app.schemas.common import ApiResponse
from app.services.badge_service import BadgeService
from app.services.user_service import UserService

router = APIRouter()


@router.get("/levels", response_model=ApiResponse[BadgeLevels])
async def badge_levels() -> ApiResponse[BadgeLevels]:
    """The ladders themselves. Public.

    `contributor_stages` lists how many approved submissions each stage of the
    tree needs; `reviewer_badges` lists how many decided submissions each
    reviewer badge needs. Use it to draw stages and badges not reached yet.

    The reviewer tiers are placeholders until they are decided, and both
    ladders can change between releases — read them from here rather than
    building the numbers into the app.
    """
    return ApiResponse.success(
        data=BadgeLevels.from_thresholds(
            settings.CONTRIBUTOR_TREE_STAGES, settings.REVIEWER_BADGE_TIERS
        )
    )


@router.get("/me", response_model=ApiResponse[BadgeSummary])
async def my_badges(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ApiResponse[BadgeSummary]:
    """Your tree and, if you review, your reviewer badges.

    **`contributor`** — one tree per person. `stage` runs from 0 (nothing
    approved yet) to `max_stage` (full-grown); draw the picture for `stage`.
    `progress` is how far you are towards the next stage and `completion` how
    far across the whole tree, both 0–1. Approved submissions made under your
    address while signed out count too.

    **`reviewer`** — every reviewer badge with `earned` set, the highest
    `level` reached, and `progress` to the next. Approving and rejecting both
    count. Null unless you are an administrator or have reviewed before.

    Errors:

    * `401` — not signed in.
    """
    summary = await BadgeService(db).summary(current_user)
    return ApiResponse.success(data=summary)


@router.get("/users/{user_id}", response_model=ApiResponse[BadgeSummary])
async def user_badges(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> ApiResponse[BadgeSummary]:
    """Admin: any account's tree and reviewer badges, shaped as `GET /badges/me`.

    Errors:

    * `403` — not an administrator.
    * `404` — no account with that id.
    """
    user = await UserService(db).get_by_id(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")
    summary = await BadgeService(db).summary(user)
    return ApiResponse.success(data=summary)
