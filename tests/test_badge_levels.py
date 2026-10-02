"""How counts turn into a tree stage and reviewer badges.

Thresholds are passed explicitly so these hold whatever the configured
ladders end up being; the reviewer tiers in particular are placeholders.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas.badge import BadgeLevels, ContributorTree, ReviewerBadges

STAGES = [1, 3, 5, 10, 20]
TIERS = [10, 50, 100]


# ── contributor tree ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("count", "stage", "next_at"),
    [
        (0, 0, 1),
        (1, 1, 3),
        (2, 1, 3),
        (3, 2, 5),
        (9, 3, 10),
        (19, 4, 20),
        (20, 5, None),
        (500, 5, None),
    ],
)
def test_the_tree_grows_a_stage_at_each_threshold(
    count: int, stage: int, next_at: int | None
) -> None:
    tree = ContributorTree.for_count(count, STAGES)
    assert (tree.stage, tree.next_stage_at, tree.max_stage) == (stage, next_at, 5)


def test_before_the_first_approval_there_is_bare_ground() -> None:
    tree = ContributorTree.for_count(0, STAGES)
    assert (tree.stage, tree.progress, tree.completion) == (0, 0.0, 0.0)


def test_progress_is_measured_from_the_current_stage_not_from_zero() -> None:
    """At 7 approvals the tree is at stage 3 (5) heading for stage 4 (10)."""
    tree = ContributorTree.for_count(7, STAGES)
    assert tree.stage == 3
    assert tree.progress == pytest.approx(0.4)  # (7 - 5) / (10 - 5)


def test_completion_lands_exactly_on_each_stage() -> None:
    """So a stage's picture and the overall bar never disagree at a threshold."""
    for stage, needed in enumerate(STAGES, start=1):
        assert ContributorTree.for_count(needed, STAGES).completion == pytest.approx(stage / 5)


def test_completion_only_ever_grows() -> None:
    values = [ContributorTree.for_count(n, STAGES).completion for n in range(0, 30)]
    assert values == sorted(values)
    assert values[-1] == 1.0


def test_a_full_grown_tree_has_nothing_left_to_reach() -> None:
    tree = ContributorTree.for_count(20, STAGES)
    assert (tree.next_stage_at, tree.progress, tree.completion) == (None, 1.0, 1.0)


# ── reviewer badges ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("count", "level", "earned"),
    [
        (0, 0, [False, False, False]),
        (9, 0, [False, False, False]),
        (10, 1, [True, False, False]),
        (99, 2, [True, True, False]),
        (100, 3, [True, True, True]),
    ],
)
def test_reviewer_badges_are_earned_at_each_tier(
    count: int, level: int, earned: list[bool]
) -> None:
    badges = ReviewerBadges.for_count(count, TIERS)
    assert badges.level == level
    assert [b.earned for b in badges.badges] == earned
    assert [b.level for b in badges.badges] == [1, 2, 3]


def test_reviewer_progress_heads_for_the_next_badge() -> None:
    badges = ReviewerBadges.for_count(30, TIERS)
    assert (badges.next_level_at, badges.progress) == (50, pytest.approx(0.5))  # (30-10)/(50-10)


def test_every_badge_earned() -> None:
    badges = ReviewerBadges.for_count(1000, TIERS)
    assert (badges.level, badges.next_level_at, badges.progress) == (3, None, 1.0)


def test_the_ladders_describe_themselves_with_nothing_earned() -> None:
    levels = BadgeLevels.from_thresholds(STAGES, TIERS)
    assert [s.approved_needed for s in levels.contributor_stages] == STAGES
    assert [s.stage for s in levels.contributor_stages] == [1, 2, 3, 4, 5]
    assert [b.reviews_needed for b in levels.reviewer_badges] == TIERS
    assert not any(b.earned for b in levels.reviewer_badges)


# ── configuration ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [[], [0, 5], [5, 5], [10, 5], [-1]])
def test_a_ladder_that_cannot_be_climbed_in_order_is_refused(bad: list[int]) -> None:
    with pytest.raises(ValidationError):
        Settings(REVIEWER_BADGE_TIERS=bad)
    with pytest.raises(ValidationError):
        Settings(CONTRIBUTOR_TREE_STAGES=bad)


def test_tiers_can_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """They are undecided; changing them must not need a code change."""
    monkeypatch.setenv("REVIEWER_BADGE_TIERS", "[3, 30, 300]")
    assert Settings().REVIEWER_BADGE_TIERS == [3, 30, 300]
