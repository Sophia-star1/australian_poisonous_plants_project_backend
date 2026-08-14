"""User administration endpoints.

Until now there was no way to read or change a role through the API at all —
`UserService.update()` supported it but nothing exposed it, so promoting an
administrator meant hand-writing SQL against the production database.

* `GET   /users/`            — admin: paginated list, optionally by role.
* `GET   /users/{id}`        — admin: single user.
* `PATCH /users/{id}/role`   — admin: change a role.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.db.session import get_db
from app.dependencies import require_admin
from app.models.user import User
from app.schemas.common import ApiResponse, PageResult
from app.schemas.role import UserRole
from app.schemas.user import UserOut, UserRoleUpdate
from app.services.user_service import UserService

router = APIRouter()


@router.get("/", response_model=ApiResponse[PageResult[UserOut]])
async def list_users(
    role: UserRole | None = Query(None, description="Filter by exact role."),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> ApiResponse[PageResult[UserOut]]:
    """Admin: list accounts and their roles."""
    rows, total = await UserService(db).list_users(page=page, page_size=page_size, role=role)
    return ApiResponse.success(
        data=PageResult[UserOut](
            items=[UserOut.model_validate(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.get("/{user_id}", response_model=ApiResponse[UserOut])
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
) -> ApiResponse[UserOut]:
    """Admin: fetch a single account."""
    user = await UserService(db).get_by_id(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")
    return ApiResponse.success(data=UserOut.model_validate(user))


@router.patch("/{user_id}/role", response_model=ApiResponse[UserOut])
async def update_user_role(
    user_id: int,
    payload: UserRoleUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ApiResponse[UserOut]:
    """Admin: promote or demote an account.

    Two guards, both about not locking yourself out:

    * an admin cannot demote themselves, which is the common accident;
    * the last remaining admin cannot be demoted by anyone, which would leave
      the instance with no way back in short of direct database access.

    The change takes effect on the target's next request — `get_current_user`
    re-reads the user rather than trusting the role in their token.
    """
    service = UserService(db)
    target = await service.get_by_id(user_id)
    if target is None:
        raise NotFoundError(f"User {user_id} not found.")

    demoting_an_admin = target.role == UserRole.ADMIN.value and payload.role != UserRole.ADMIN
    if demoting_an_admin:
        if target.id == admin.id:
            raise ForbiddenError("You cannot remove your own admin role.")
        if await service.count_with_role(UserRole.ADMIN) <= 1:
            raise ForbiddenError("Cannot demote the last remaining administrator.")

    updated = await service.set_role(user_id, payload.role)
    if updated is None:  # pragma: no cover - the row was deleted mid-request
        raise NotFoundError(f"User {user_id} not found.")
    return ApiResponse.success(data=UserOut.model_validate(updated))
