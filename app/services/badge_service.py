"""Counting the work a badge stands for.

Both counts come from `submissions`, which already records who contributed a
submission (`contributor_email`), who decided it (`reviewed_by`) and what was
decided (`status`).

Matching is case-insensitive at the query. Addresses are lowercased when an
account is created and when a submission is stored, so in practice they agree
already, but `reviewed_by` is copied from the account row at review time
without being normalised again, and a badge that silently reads zero because
of a capital letter would be very hard to explain to the person missing it.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.submission import Submission
from app.models.user import User
from app.schemas.badge import BadgeSummary, ContributorTree, ReviewerBadges
from app.schemas.role import UserRole

APPROVED = "approved"
#: A submission counts towards its reviewer once it has been decided either
#: way. A rejection is as much work as an approval — often more, since it has
#: to be explained to the contributor.
DECIDED = ("approved", "rejected")


class BadgeService:
    """Read-only: badges are counted, never written."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def counts(self, email: str) -> tuple[int, int]:
        """(approved submissions contributed, submissions decided) for `email`.

        One query for both: the database is a ~70 ms round trip away from the
        service, so two separate counts would double the cost of a profile.
        Anonymous submissions made under the same address count towards the
        contributor tree, the same way they appear in `GET /submissions/me`.
        """
        address = email.lower()
        contributed = func.lower(Submission.contributor_email) == address
        reviewed = func.lower(Submission.reviewed_by) == address
        row = (
            await self.db.execute(
                select(
                    func.count().filter(contributed, Submission.status == APPROVED),
                    func.count().filter(reviewed, Submission.status.in_(DECIDED)),
                ).where(or_(contributed, reviewed))
            )
        ).one()
        return int(row[0] or 0), int(row[1] or 0)

    async def summary(self, user: User) -> BadgeSummary:
        """Both badges for one account, graded against the current settings.

        The reviewer section is included for administrators, and for anyone
        who has reviewed before: a reviewer who loses the role keeps what they
        earned. For everyone else it is null rather than a ladder at zero, so
        a contributor's profile is not shown badges they cannot work towards.
        """
        approved, reviewed = await self.counts(user.email)
        shows_reviewer = user.role == UserRole.ADMIN.value or reviewed > 0
        return BadgeSummary(
            user_id=user.id,
            contributor=ContributorTree.for_count(approved, settings.CONTRIBUTOR_TREE_STAGES),
            reviewer=(
                ReviewerBadges.for_count(reviewed, settings.REVIEWER_BADGE_TIERS)
                if shows_reviewer
                else None
            ),
        )
