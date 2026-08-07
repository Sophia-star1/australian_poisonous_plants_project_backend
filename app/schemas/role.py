"""Application roles.

`app_users.role` is an unconstrained VARCHAR and `require_role` compares it
with `==`, so a typo anywhere produces a permanent, silent 403 rather than an
error. Naming the values in one place lets the type checker and pydantic catch
that at the boundary instead.
"""

from enum import StrEnum


class UserRole(StrEnum):
    """Roles a user account can hold.

    `StrEnum` so the members compare equal to the plain strings already stored
    in the database and serialise unchanged in JSON and JWT claims — this is
    additive, not a data migration.
    """

    USER = "user"
    ADMIN = "admin"


#: Assigned to every new account; see AuthService.sign_up.
DEFAULT_ROLE = UserRole.USER
