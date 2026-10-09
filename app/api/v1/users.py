"""User administration endpoints.

Until now there was no way to read or change a role through the API at all —
`UserService.update()` supported it but nothing exposed it, so promoting an
administrator meant hand-writing SQL against the production database.

* `GET   /users/`            — admin: paginated list, optionally by role.
* `GET   /users/{id}`        — admin: single user.
* `PATCH /users/{id}/role`   — admin: change a role.

And for every signed-in user, their own profile:

* `PATCH  /users/me`         — change your display name.
* `PUT    /users/me/avatar`  — upload or replace the profile picture.
* `DELETE /users/me/avatar`  — remove it.
"""

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import ForbiddenError, NotFoundError, UnprocessableError
from app.db.session import get_db
from app.dependencies import get_current_active_user, require_admin
from app.ml.classifier import ImageTooLargeError, UndecodableImageError
from app.models.user import User
from app.schemas.common import ApiResponse, PageResult
from app.schemas.role import UserRole
from app.schemas.user import UserNameUpdate, UserOut, UserRoleUpdate
from app.services.avatar_service import AvatarService
from app.services.storage_service import StorageError
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
    """Admin: one page of accounts and their roles.

    `role` filters to an exact role. `page` starts at 1 and `page_size` is
    capped at 100; the response carries `total` for building a pager.
    """
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
    """Admin: one account.

    Errors:

    * `404` — no account with that id.
    """
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

    Takes effect on the target's very next request, including one made with a
    token issued before the change — permission is read from the account, not
    from the token.

    Two things are refused, both about not locking everyone out: you cannot
    demote yourself, and the last remaining administrator cannot be demoted by
    anyone. Recovering from either would mean editing the database by hand.

    Errors:

    * `403` — demoting yourself, or demoting the last administrator.
    * `404` — no account with that id.
    * `422` — a role other than `user` or `admin`.
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


@router.patch("/me", response_model=ApiResponse[UserOut])
async def update_my_name(
    payload: UserNameUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ApiResponse[UserOut]:
    """Change your own display name.

    Shows on the profile screen and becomes the `contributor_name` on
    submissions made while signed in. Submissions already recorded keep the
    name they were made with — each stores its own copy, so renaming does
    not rewrite history.

    Errors:

    * `401` — not signed in.
    * `422` — a blank name, one longer than 255 characters, or an unknown
      field. `extra="forbid"` is deliberate: silently ignoring a stray
      `email`, `password` or `role` here would look like a security hole
      even when nothing is stored.
    """
    updated = await UserService(db).set_full_name(current_user.id, payload.full_name)
    if updated is None:  # pragma: no cover - the account cannot vanish mid-request
        raise NotFoundError("Account not found.")
    return ApiResponse.success(data=UserOut.model_validate(updated))


@router.put("/me/avatar", response_model=ApiResponse[UserOut])
async def set_my_avatar(
    file: UploadFile = File(..., description="The picture: JPEG, PNG or WebP."),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ApiResponse[UserOut]:
    """Upload or replace your profile picture.

    Send one image under `file`: JPEG, PNG or WebP, within the upload size
    limit. It is centre-cropped to a square, resized to 512x512 and
    stored as a JPEG, with its metadata (including any GPS position) removed.
    Returns your account with the new `avatar_url`; the URL changes with
    every upload, and the previous picture is deleted.

    Errors:

    * `401` — not signed in.
    * `422` — not JPEG, PNG or WebP, not a readable image, too large a file,
      or too many pixels to process.
    """
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in settings.ALLOWED_IMAGE_TYPES:
        allowed = ", ".join(settings.ALLOWED_IMAGE_TYPES)
        raise UnprocessableError(
            f"Unsupported type '{content_type or 'unknown'}'. Allowed: {allowed}."
        )

    service = AvatarService(db)
    try:
        previous = await service.replace(current_user, file)
    except ImageTooLargeError as exc:
        raise UnprocessableError(
            f"The picture is too large to process ({exc}). Export it at a smaller "
            "size, or as a JPEG, and try again."
        ) from exc
    except UndecodableImageError as exc:
        raise UnprocessableError("The file is not a readable image.") from exc
    except StorageError as exc:
        # Too large a file, or storage unavailable.
        raise UnprocessableError(str(exc)) from exc

    out = UserOut.model_validate(current_user)
    # Committed before answering, and before the old picture is deleted: a
    # failed commit leaves the account on its old avatar, so the new upload is
    # the one to take back.
    try:
        await db.commit()
    except Exception:
        await service.discard_upload()
        raise
    if previous:
        await service.delete_file(previous)
    return ApiResponse.success(data=out, message="Avatar updated.")


@router.delete("/me/avatar", response_model=ApiResponse[UserOut])
async def remove_my_avatar(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ApiResponse[UserOut]:
    """Remove your profile picture; `avatar_url` becomes null.

    Removing one you do not have is not an error.

    Errors:

    * `401` — not signed in.
    """
    service = AvatarService(db)
    previous = await service.remove(current_user)
    out = UserOut.model_validate(current_user)
    await db.commit()
    if previous:
        await service.delete_file(previous)
    return ApiResponse.success(data=out, message="Avatar removed.")
