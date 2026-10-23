"""Tests for the admin screening-threshold endpoints and their wiring.

Covers the full loop an admin adjustment takes: the schema at the boundary,
the service that persists the pair and applies it to the process, the
classifier grading against the runtime override, and the endpoints that
require the admin role.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app import dependencies
from app.api.v1 import admin as admin_api
from app.config import settings
from app.core.security import create_access_token
from app.ml import classifier as ml
from app.models.user import User
from app.schemas.role import UserRole
from app.schemas.screening import ScreeningConfigUpdate
from app.services.screening_config_service import (
    ScreeningConfigService,
    reset_refresh_throttle,
)


def _user(user_id: int, role: str, email: str | None = None) -> User:
    """Build a detached User instance; no session or database involved."""
    user = User()
    user.id = user_id
    user.email = email or f"user{user_id}@example.com"
    user.is_active = True
    user.role = role
    return user


def _auth(user_id: int = 1) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user_id)})}"}


@pytest.fixture
def as_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authenticate every request as an administrator (id 1)."""

    async def _get_by_id(self: Any, wanted: int) -> User:
        return _user(wanted, UserRole.ADMIN.value, email="admin@example.com")

    monkeypatch.setattr(dependencies.UserService, "get_by_id", _get_by_id)


@pytest.fixture(autouse=True)
def _clean_runtime_thresholds() -> Any:
    """No test may leak a runtime override into the next one."""
    yield
    ml.clear_runtime_thresholds()


# ── the schema ───────────────────────────────────────────────────────────────


def test_thresholds_must_be_within_the_unit_interval() -> None:
    """Values outside [0, 1] are rejected at the boundary."""
    with pytest.raises(ValidationError):
        ScreeningConfigUpdate(reject_below=1.2)
    with pytest.raises(ValidationError):
        ScreeningConfigUpdate(accept_at=-0.1)
    assert ScreeningConfigUpdate(reject_below=0.0).reject_below == 0.0
    assert ScreeningConfigUpdate(accept_at=1.0).accept_at == 1.0


def test_unknown_fields_are_rejected() -> None:
    """extra=forbid: a stray field is a validation error, not a silent no-op."""
    with pytest.raises(ValidationError):
        ScreeningConfigUpdate(reject_below=0.6, accept_at=0.9, pass_everything=True)  # type: ignore[call-arg]


# ── the classifier grades by the runtime override ────────────────────────────


class _StubClassifier(ml.PlantClassifier):
    """Scores a fixed probability, so only the band boundaries are tested."""

    def __init__(self) -> None:
        self.reject_below = 0.10
        self.accept_at = 0.70

    def probability(self, data: bytes) -> float:
        return 0.60


def test_without_an_override_the_instance_bands_apply() -> None:
    """A directly-constructed classifier keeps the bands it was given."""
    assert _StubClassifier().classify(b"x").decision is ml.Decision.FLAG


def test_a_runtime_override_replaces_the_instance_bands() -> None:
    """The whole point: an admin change applies without a reload."""
    ml.set_runtime_thresholds(0.80, 0.90)
    assert _StubClassifier().classify(b"x").decision is ml.Decision.REJECT

    ml.set_runtime_thresholds(0.20, 0.55)
    assert _StubClassifier().classify(b"x").decision is ml.Decision.ACCEPT


def test_a_reversed_override_is_refused() -> None:
    """A pair that rejects what it accepts can never be set."""
    with pytest.raises(ValueError, match="reject_below"):
        ml.set_runtime_thresholds(0.9, 0.2)
    assert ml.runtime_thresholds() is None


# ── the service ──────────────────────────────────────────────────────────────


class _DB:
    """Enough of a session for the service; records what it was asked."""

    def __init__(self, row: Any = None, fail: bool = False) -> None:
        self.row = row
        self.fail = fail
        self.calls = 0
        self.added: list[Any] = []

    async def execute(self, stmt: Any) -> _Result:
        self.calls += 1
        if self.fail:
            raise RuntimeError("database unavailable")
        return _Result(self.row)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


class _Result:
    """Mimics a SQLAlchemy result just far enough for `.scalars().first()`."""

    def __init__(self, row: Any) -> None:
        self._row = row

    def scalars(self) -> _Scalars:
        return _Scalars(self._row)


class _Scalars:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row


class _Row:
    def __init__(self, reject_below: float, accept_at: float) -> None:
        self.reject_below = reject_below
        self.accept_at = accept_at
        self.updated_at = None
        self.updated_by = ""


@pytest.mark.asyncio
async def test_refresh_applies_the_row_and_is_throttled() -> None:
    """One read per cache window, whatever the upload volume."""
    reset_refresh_throttle()
    db = _DB(row=_Row(0.55, 0.75))
    service = ScreeningConfigService(db)  # type: ignore[arg-type]

    await service.refresh()
    assert ml.runtime_thresholds() == (0.55, 0.75)

    await service.refresh()
    assert db.calls == 1, "a second refresh inside the window must not re-read"


@pytest.mark.asyncio
async def test_refresh_without_a_row_restores_the_defaults() -> None:
    """A fresh database behaves exactly like the code did before the table."""
    reset_refresh_throttle()
    ml.set_runtime_thresholds(0.60, 0.80)

    await ScreeningConfigService(_DB(row=None)).refresh()  # type: ignore[arg-type]

    assert ml.runtime_thresholds() is None


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_what_is_in_force() -> None:
    """The config store is a convenience; an upload must never fail for it."""
    reset_refresh_throttle()
    ml.set_runtime_thresholds(0.60, 0.80)

    await ScreeningConfigService(_DB(fail=True)).refresh()  # type: ignore[arg-type]

    assert ml.runtime_thresholds() == (0.60, 0.80)


@pytest.mark.asyncio
async def test_update_creates_the_row_and_applies_immediately() -> None:
    db = _DB(row=None)
    service = ScreeningConfigService(db)  # type: ignore[arg-type]

    row = await service.update(0.45, 0.65, updated_by="admin@example.com")

    assert len(db.added) == 1
    assert (row.reject_below, row.accept_at, row.updated_by) == (0.45, 0.65, "admin@example.com")
    assert row.updated_at is not None
    assert ml.runtime_thresholds() == (0.45, 0.65)


@pytest.mark.asyncio
async def test_update_rewrites_an_existing_row() -> None:
    db = _DB(row=_Row(0.50, 0.70))
    service = ScreeningConfigService(db)  # type: ignore[arg-type]

    row = await service.update(0.55, 0.70, updated_by="admin@example.com")

    assert db.added == []
    assert (row.reject_below, row.accept_at) == (0.55, 0.70)
    assert ml.runtime_thresholds() == (0.55, 0.70)


# ── the endpoints ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_requires_a_token(async_client: AsyncClient) -> None:
    """Anonymous callers get 401."""
    assert (await async_client.get("/api/v1/admin/screening")).status_code == 401


@pytest.mark.asyncio
async def test_get_requires_admin(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A normal account cannot read the tuning knobs."""

    async def _get_by_id(self: Any, wanted: int) -> User:
        return _user(wanted, UserRole.USER.value)

    monkeypatch.setattr(dependencies.UserService, "get_by_id", _get_by_id)

    response = await async_client.get("/api/v1/admin/screening", headers=_auth())
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_get_reports_defaults_until_an_admin_changes_them(
    async_client: AsyncClient, as_admin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the first PATCH, the configured settings govern."""

    class _FakeService:
        def __init__(self, db: Any) -> None:
            pass

        async def get(self) -> None:
            return None

    monkeypatch.setattr(admin_api, "ScreeningConfigService", _FakeService)

    response = await async_client.get("/api/v1/admin/screening", headers=_auth())
    data = response.json()["data"]
    assert data["source"] == "defaults"
    assert data["reject_below"] == settings.PLANT_REJECT_BELOW
    assert data["accept_at"] == settings.PLANT_ACCEPT_AT


@pytest.mark.asyncio
async def test_patch_applies_the_merged_pair(
    async_client: AsyncClient, as_admin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One field sent, the other kept; the change is recorded and applied."""
    captured: dict[str, Any] = {}

    class _Row:
        def __init__(self, reject_below: float, accept_at: float) -> None:
            self.reject_below = reject_below
            self.accept_at = accept_at
            self.updated_at = None
            self.updated_by = "admin@example.com"

    class _FakeService:
        def __init__(self, db: Any) -> None:
            pass

        async def get(self) -> _Row:
            return _Row(0.50, 0.70)

        async def update(self, reject_below: float, accept_at: float, updated_by: str) -> _Row:
            captured.update(reject_below=reject_below, accept_at=accept_at, updated_by=updated_by)
            return _Row(reject_below, accept_at)

    monkeypatch.setattr(admin_api, "ScreeningConfigService", _FakeService)

    response = await async_client.patch(
        "/api/v1/admin/screening", json={"reject_below": 0.60}, headers=_auth()
    )

    assert response.status_code == 200
    assert response.json()["data"]["source"] == "database"
    assert response.json()["data"]["reject_below"] == 0.60
    assert captured == {"reject_below": 0.60, "accept_at": 0.70, "updated_by": "admin@example.com"}


@pytest.mark.asyncio
async def test_patch_rejects_a_reversed_pair(
    async_client: AsyncClient, as_admin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering rule is checked against the merged pair, not each field."""

    class _FakeService:
        def __init__(self, db: Any) -> None:
            pass

        async def get(self) -> None:
            return None

    monkeypatch.setattr(admin_api, "ScreeningConfigService", _FakeService)

    response = await async_client.patch(
        "/api/v1/admin/screening", json={"reject_below": 0.9, "accept_at": 0.8}, headers=_auth()
    )
    assert response.status_code == 422
    assert "reject_below" in response.json()["message"]


@pytest.mark.asyncio
async def test_patch_rejects_an_unknown_field(
    async_client: AsyncClient, as_admin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray field must not be silently dropped."""

    class _FakeService:
        def __init__(self, db: Any) -> None:
            pass

        async def get(self) -> None:
            return None

    monkeypatch.setattr(admin_api, "ScreeningConfigService", _FakeService)

    response = await async_client.patch(
        "/api/v1/admin/screening",
        json={"reject_below": 0.6, "accept_at": 0.9, "disable_screening": True},
        headers=_auth(),
    )
    assert response.status_code == 422
