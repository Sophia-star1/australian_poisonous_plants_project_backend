"""The badge endpoints and the counting behind them.

Counts are stubbed at `BadgeService.counts` for the endpoint cases; the query
itself is checked separately, as compiled SQL, for what it filters on.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.dialects import postgresql

from app.config import settings
from app.db.session import get_db
from app.dependencies import get_current_active_user
from app.main import app
from app.models.user import User
from app.services.badge_service import BadgeService
from app.services.user_service import UserService


def _user(role: str = "user", user_id: int = 7, email: str = "ada@example.com") -> User:
    user = User()
    user.id = user_id
    user.email = email
    user.full_name = "Ada"
    user.is_active = True
    user.role = role
    return user


@pytest.fixture(autouse=True)
def ladders(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fixed ladders, so these do not move when the defaults are tuned."""
    monkeypatch.setattr(settings, "CONTRIBUTOR_TREE_STAGES", [1, 3, 5, 10, 20])
    monkeypatch.setattr(settings, "REVIEWER_BADGE_TIERS", [10, 50, 100])

    async def _no_db() -> Any:
        yield None

    app.dependency_overrides[get_db] = _no_db
    yield
    app.dependency_overrides.clear()


def _counts(monkeypatch: pytest.MonkeyPatch, approved: int, reviewed: int) -> list[str]:
    asked: list[str] = []

    async def _fake(self: Any, email: str) -> tuple[int, int]:
        asked.append(email)
        return approved, reviewed

    monkeypatch.setattr(BadgeService, "counts", _fake)
    return asked


def _sign_in(user: User) -> None:
    app.dependency_overrides[get_current_active_user] = lambda: user


# ── /badges/levels ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_levels_are_public_and_follow_the_settings(async_client: AsyncClient) -> None:
    response = await async_client.get("/api/v1/badges/levels")
    assert response.status_code == 200
    data = response.json()["data"]
    assert [s["approved_needed"] for s in data["contributor_stages"]] == [1, 3, 5, 10, 20]
    assert [b["reviews_needed"] for b in data["reviewer_badges"]] == [10, 50, 100]


# ── /badges/me ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_me_requires_signing_in(async_client: AsyncClient) -> None:
    app.dependency_overrides.pop(get_current_active_user, None)
    response = await async_client.get("/api/v1/badges/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_a_contributor_gets_a_tree_and_no_reviewer_ladder(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _counts(monkeypatch, approved=7, reviewed=0)
    _sign_in(_user())

    response = await async_client.get("/api/v1/badges/me")

    assert response.status_code == 200
    data = response.json()["data"]
    assert asked == ["ada@example.com"]
    assert data["user_id"] == 7
    assert data["contributor"] == {
        "approved_count": 7,
        "stage": 3,
        "max_stage": 5,
        "next_stage_at": 10,
        "progress": 0.4,
        "completion": 0.68,
    }
    assert data["reviewer"] is None


@pytest.mark.asyncio
async def test_an_admin_sees_the_reviewer_ladder_even_at_zero(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """So a new reviewer can see what there is to earn."""
    _counts(monkeypatch, approved=0, reviewed=0)
    _sign_in(_user(role="admin"))

    data = (await async_client.get("/api/v1/badges/me")).json()["data"]

    assert data["reviewer"]["level"] == 0
    assert data["reviewer"]["next_level_at"] == 10
    assert [b["earned"] for b in data["reviewer"]["badges"]] == [False, False, False]


@pytest.mark.asyncio
async def test_a_demoted_reviewer_keeps_what_they_earned(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _counts(monkeypatch, approved=2, reviewed=60)
    _sign_in(_user(role="user"))

    data = (await async_client.get("/api/v1/badges/me")).json()["data"]

    assert data["reviewer"]["level"] == 2
    assert [b["earned"] for b in data["reviewer"]["badges"]] == [True, True, False]


@pytest.mark.asyncio
async def test_retuned_tiers_regrade_without_any_migration(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tiers are undecided; changing them must re-grade existing reviewers."""
    _counts(monkeypatch, approved=0, reviewed=12)
    _sign_in(_user(role="admin"))
    assert (await async_client.get("/api/v1/badges/me")).json()["data"]["reviewer"]["level"] == 1

    monkeypatch.setattr(settings, "REVIEWER_BADGE_TIERS", [5, 12, 40])
    assert (await async_client.get("/api/v1/badges/me")).json()["data"]["reviewer"]["level"] == 2


# ── /badges/users/{id} ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_admin_can_read_anyones_badges(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _counts(monkeypatch, approved=20, reviewed=0)

    async def _found(self: Any, user_id: int) -> User:
        return _user(user_id=user_id, email="bo@example.com")

    monkeypatch.setattr(UserService, "get_by_id", _found)
    _sign_in(_user(role="admin", user_id=1, email="admin@example.com"))

    response = await async_client.get("/api/v1/badges/users/42")

    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["user_id"], data["contributor"]["stage"]) == (42, 5)
    assert asked == ["bo@example.com"], "counted the admin instead of the target"


@pytest.mark.asyncio
async def test_reading_someone_elses_badges_needs_admin(async_client: AsyncClient) -> None:
    _sign_in(_user(role="user"))
    response = await async_client.get("/api/v1/badges/users/42")
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_an_unknown_account_is_404(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _missing(self: Any, user_id: int) -> None:
        return None

    monkeypatch.setattr(UserService, "get_by_id", _missing)
    _sign_in(_user(role="admin"))
    response = await async_client.get("/api/v1/badges/users/999")
    assert response.status_code == 404


# ── the query ────────────────────────────────────────────────────────────────


class _CapturingSession:
    def __init__(self) -> None:
        self.statement: Any = None

    async def execute(self, statement: Any) -> Any:
        self.statement = statement

        class _Row:
            def one(self) -> tuple[int, int]:
                return (3, 11)

        return _Row()


@pytest.mark.asyncio
async def test_one_query_counts_approvals_and_decisions_case_insensitively() -> None:
    session = _CapturingSession()
    counts = await BadgeService(session).counts("Ada@Example.com")  # type: ignore[arg-type]

    assert counts == (3, 11)
    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    flat = " ".join(sql.split())
    assert "lower(submissions.contributor_email) = 'ada@example.com'" in flat
    assert "lower(submissions.reviewed_by) = 'ada@example.com'" in flat
    # Contributions count only once approved; reviews count either way.
    assert "submissions.status = 'approved'" in flat
    assert "submissions.status IN ('approved', 'rejected')" in flat
    # Pending submissions a reviewer has not decided must not count.
    assert "'pending'" not in flat
