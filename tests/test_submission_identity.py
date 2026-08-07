"""Tests for contributor identity on submissions.

`POST /submissions` used to trust a client-supplied `contributor_email`, while
`GET /submissions/me` matches submissions on that same field — so a caller
could place a record into another person's list by typing their address.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.api.v1 import submissions as submissions_api
from app.core.security import create_access_token
from app.dependencies import get_optional_user
from app.main import app
from app.models.user import User


def _user(email: str, full_name: str | None = "Real Name", is_active: bool = True) -> User:
    """Build a detached User instance; no session or database involved."""
    user = User()
    user.id = 1
    user.email = email
    user.full_name = full_name
    user.is_active = is_active
    user.role = "user"
    return user


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture what the endpoint hands to the service, skipping storage."""
    seen: dict[str, Any] = {}

    class _Row:
        id = uuid4()
        contributor_name = ""
        contributor_email = ""
        submitted_at = datetime.now(timezone.utc)
        status = "pending"
        user_description = None
        location_state = None
        latitude = None
        longitude = None
        images: list[Any] = []

    async def _create(self: Any, payload: Any, photos: Any) -> Any:
        seen["payload"] = payload
        row = _Row()
        row.contributor_name = payload.contributor_name
        row.contributor_email = payload.contributor_email
        return row

    monkeypatch.setattr(submissions_api.SubmissionService, "create", _create)
    return seen


def _multipart() -> dict[str, Any]:
    """Minimal valid multipart body with one photo."""
    return {
        "files": {"photos": ("p.jpg", b"fake-bytes", "image/jpeg")},
        "data": {
            "contributor_name": "Attacker",
            "contributor_email": "victim@example.com",
            "user_description": "",
        },
    }


@pytest.mark.asyncio
async def test_anonymous_submission_keeps_form_fields(
    async_client: AsyncClient, captured: dict[str, Any]
) -> None:
    """Without a token the form values are still used — anonymous is allowed."""
    app.dependency_overrides[get_optional_user] = lambda: None
    try:
        body = _multipart()
        response = await async_client.post(
            "/api/v1/submissions/", files=body["files"], data=body["data"]
        )
    finally:
        app.dependency_overrides.pop(get_optional_user, None)

    assert response.status_code == 201
    assert captured["payload"].contributor_email == "victim@example.com"


@pytest.mark.asyncio
async def test_signed_in_submission_ignores_supplied_email(
    async_client: AsyncClient, captured: dict[str, Any]
) -> None:
    """Regression: the token's identity wins over the form field."""
    app.dependency_overrides[get_optional_user] = lambda: _user("real@example.com")
    try:
        body = _multipart()
        response = await async_client.post(
            "/api/v1/submissions/",
            files=body["files"],
            data=body["data"],
            headers={"Authorization": f"Bearer {create_access_token({'sub': '1'})}"},
        )
    finally:
        app.dependency_overrides.pop(get_optional_user, None)

    assert response.status_code == 201
    payload = captured["payload"]
    assert payload.contributor_email == "real@example.com", "spoofed email was accepted"
    assert payload.contributor_name == "Real Name"


@pytest.mark.asyncio
async def test_signed_in_without_full_name_falls_back_to_email_local_part(
    async_client: AsyncClient, captured: dict[str, Any]
) -> None:
    """A user who never set a display name still gets a sensible attribution."""
    app.dependency_overrides[get_optional_user] = lambda: _user("someone@example.com", None)
    try:
        body = _multipart()
        response = await async_client.post(
            "/api/v1/submissions/", files=body["files"], data=body["data"]
        )
    finally:
        app.dependency_overrides.pop(get_optional_user, None)

    assert response.status_code == 201
    assert captured["payload"].contributor_name == "someone"
