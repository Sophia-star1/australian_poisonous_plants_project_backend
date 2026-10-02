"""Client input that used to reach the database and come back as a 500.

Each of these passed validation, then broke a column constraint the schema
did not know about — NOT NULL, VARCHAR(255), a unique index — or was stored
as data nobody meant: a name of spaces, a latitude of 999.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError
from app.schemas.auth import SignUpRequest
from app.schemas.submission import SubmissionContentUpdate, SubmissionCreate
from app.services.auth_service import AuthService
from app.services.user_service import UserService


def _create(**overrides: Any) -> SubmissionCreate:
    fields: dict[str, Any] = {"contributor_name": "Ada", "contributor_email": "ada@example.com"}
    fields.update(overrides)
    return SubmissionCreate(**fields)


# ── editing a submission ─────────────────────────────────────────────────────


def test_a_null_name_is_refused_rather_than_written() -> None:
    """`contributor_name` is NOT NULL; null used to reach the UPDATE as a 500."""
    with pytest.raises(ValidationError, match="cannot be null"):
        SubmissionContentUpdate.model_validate({"contributor_name": None})


def test_an_omitted_name_is_still_fine() -> None:
    payload = SubmissionContentUpdate.model_validate({"location_state": "QLD"})
    assert payload.model_dump(exclude_unset=True) == {"location_state": "QLD"}


@pytest.mark.parametrize("name", ["", "   "])
def test_a_blank_name_is_refused_on_edit(name: str) -> None:
    with pytest.raises(ValidationError):
        SubmissionContentUpdate.model_validate({"contributor_name": name})


def test_names_are_trimmed_on_edit() -> None:
    payload = SubmissionContentUpdate.model_validate({"contributor_name": "  Ada  "})
    assert payload.contributor_name == "Ada"


@pytest.mark.parametrize("field", ["user_description", "location_state"])
def test_a_blank_answer_clears_like_null(field: str) -> None:
    """Upload stores an empty description as NULL; editing now agrees."""
    payload = SubmissionContentUpdate.model_validate({field: "   "})
    assert payload.model_dump(exclude_unset=True) == {field: None}


# ── creating a submission ────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["", "   "])
def test_a_blank_name_is_refused_on_upload(name: str) -> None:
    """`"   "` passed `min_length=1` and was stored as an empty string."""
    with pytest.raises(ValidationError):
        _create(contributor_name=name)


# ── coordinates ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "coords",
    [{"latitude": 90.0001}, {"latitude": -999}, {"longitude": 180.5}, {"longitude": -5000}],
)
def test_coordinates_outside_the_globe_are_refused(coords: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        _create(**coords)
    with pytest.raises(ValidationError):
        SubmissionContentUpdate.model_validate(coords)


def test_the_extremes_themselves_are_valid() -> None:
    point = _create(latitude=-90, longitude=180)
    assert (point.latitude, point.longitude) == (-90, 180)
    _create(latitude=90, longitude=-180)


# ── signing up ───────────────────────────────────────────────────────────────


def test_a_name_longer_than_the_column_is_refused() -> None:
    """`app_users.full_name` is VARCHAR(255); a longer one was a 500 at INSERT."""
    SignUpRequest(email="a@example.com", password="a-valid-password", full_name="n" * 255)
    with pytest.raises(ValidationError):
        SignUpRequest(email="a@example.com", password="a-valid-password", full_name="n" * 256)


@pytest.mark.parametrize(("given", "stored"), [("  Ada  ", "Ada"), ("   ", None), ("", None)])
def test_signup_names_are_trimmed_and_blank_means_none(given: str, stored: str | None) -> None:
    request = SignUpRequest(email="a@example.com", password="a-valid-password", full_name=given)
    assert request.full_name == stored


@pytest.mark.asyncio
async def test_losing_a_signup_race_is_a_409_not_a_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both requests pass the lookup; the unique index refuses the second INSERT."""

    async def _not_found(self: Any, email: str) -> None:
        return None

    async def _duplicate(self: Any, payload: Any) -> None:
        raise IntegrityError("INSERT", {}, Exception("duplicate key value"))

    monkeypatch.setattr(UserService, "get_by_email", _not_found)
    monkeypatch.setattr(UserService, "create", _duplicate)

    with pytest.raises(ConflictError) as excinfo:
        await AuthService(db=None).sign_up(  # type: ignore[arg-type]
            SignUpRequest(email="race@example.com", password="a-valid-password")
        )
    # The same words the lookup path uses, so neither reveals which happened.
    assert "could not create the account" in str(excinfo.value.detail).lower()
