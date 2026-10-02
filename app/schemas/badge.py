"""Contributor trees and reviewer badges.

Two separate things, because they reward different work:

* **Contributor** — one badge per person: a tree that grows as their
  submissions are approved. The thresholds are `CONTRIBUTOR_TREE_STAGES`; the
  frontend draws one picture per stage and can animate between them with
  `progress`.
* **Reviewer** — several badges, one per tier in `REVIEWER_BADGE_TIERS`,
  earned by deciding submissions. Approving and rejecting both count.

## Counted, not stored

Both are derived at read time from `submissions` — approved rows for a
contributor, decided rows for a reviewer — rather than kept in a table.

The database has no migration history, so a new table would mean editing
production by hand. A stored count is also a second copy of a fact that can
drift from the first. And because the reviewer tiers are still undecided,
counting means changing them re-grades everyone at once instead of leaving
early awards stuck at the old numbers. What it costs is the date a badge was
earned, which nothing asks for yet.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field


def _level(count: int, thresholds: Sequence[int]) -> int:
    """How many thresholds `count` has reached: 0 for none, len() for all."""
    return sum(1 for threshold in thresholds if count >= threshold)


def _progress(count: int, level: int, thresholds: Sequence[int]) -> float:
    """Fraction of the way from the current level to the next, 0 to 1.

    1.0 at the top, where there is no next level to work towards.
    """
    if level >= len(thresholds):
        return 1.0
    floor = thresholds[level - 1] if level > 0 else 0
    ceiling = thresholds[level]
    return round((count - floor) / (ceiling - floor), 4)


class TreeStage(BaseModel):
    """One growth stage of the contributor tree."""

    stage: int = Field(description="1-based; stage 0 is before the first approval.")
    approved_needed: int


class ContributorTree(BaseModel):
    """A contributor's single badge: how grown their tree is."""

    approved_count: int = Field(description="Approved submissions credited to this account.")
    stage: int = Field(description="Current growth stage, 0 (nothing yet) to `max_stage`.")
    max_stage: int = Field(description="The full-grown tree.")
    next_stage_at: int | None = Field(
        description="Approved submissions needed for the next stage; null when full-grown."
    )
    progress: float = Field(
        description="0–1, how far from the current stage to the next; 1.0 when full-grown."
    )
    completion: float = Field(
        description="0–1 across the whole tree: (stage + progress) / max_stage. "
        "Equals stage / max_stage exactly at each threshold."
    )

    @classmethod
    def for_count(cls, approved_count: int, stages: Sequence[int]) -> ContributorTree:
        stage = _level(approved_count, stages)
        progress = _progress(approved_count, stage, stages)
        top = len(stages)
        return cls(
            approved_count=approved_count,
            stage=stage,
            max_stage=top,
            next_stage_at=stages[stage] if stage < top else None,
            progress=progress,
            completion=1.0 if stage >= top else round((stage + progress) / top, 4),
        )


class ReviewerBadge(BaseModel):
    """One reviewer badge, earned or not yet."""

    level: int = Field(description="1-based; the frontend maps it to artwork and a name.")
    reviews_needed: int
    earned: bool


class ReviewerBadges(BaseModel):
    """Everything a reviewer has earned and what comes next."""

    reviewed_count: int = Field(description="Submissions this account has approved or rejected.")
    level: int = Field(description="Highest badge earned, 0 for none.")
    next_level_at: int | None = Field(
        description="Reviews needed for the next badge; null once all are earned."
    )
    progress: float = Field(description="0–1 towards the next badge; 1.0 once all are earned.")
    badges: list[ReviewerBadge] = Field(description="Every tier, in order, with `earned` set.")

    @classmethod
    def for_count(cls, reviewed_count: int, tiers: Sequence[int]) -> ReviewerBadges:
        level = _level(reviewed_count, tiers)
        return cls(
            reviewed_count=reviewed_count,
            level=level,
            next_level_at=tiers[level] if level < len(tiers) else None,
            progress=_progress(reviewed_count, level, tiers),
            badges=[
                ReviewerBadge(level=i, reviews_needed=needed, earned=reviewed_count >= needed)
                for i, needed in enumerate(tiers, start=1)
            ],
        )


class BadgeSummary(BaseModel):
    """One account's standing."""

    user_id: int
    contributor: ContributorTree
    reviewer: ReviewerBadges | None = Field(
        description="Present for administrators, and for anyone who has reviewed before "
        "(a demoted reviewer keeps what they earned). Null otherwise."
    )


class BadgeLevels(BaseModel):
    """The ladders themselves, so a client can draw locked stages and badges."""

    contributor_stages: list[TreeStage]
    reviewer_badges: list[ReviewerBadge] = Field(
        description="`earned` is always false here; this describes the tiers, not a person."
    )

    @classmethod
    def from_thresholds(cls, stages: Sequence[int], tiers: Sequence[int]) -> BadgeLevels:
        return cls(
            contributor_stages=[
                TreeStage(stage=i, approved_needed=needed)
                for i, needed in enumerate(stages, start=1)
            ],
            reviewer_badges=[
                ReviewerBadge(level=i, reviews_needed=needed, earned=False)
                for i, needed in enumerate(tiers, start=1)
            ],
        )
