"""Photo uploads are rate limited, per client IP and per signed-in account.

Submitting needs no account and a request may carry ten 10 MB photos, so an
unlimited endpoint lets one client fill the storage. The service is stubbed
to fail fast: the limit is checked before it is called, which is the property
under test.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from starlette.requests import Request

from app.api.v1 import submissions
from app.config import settings
from app.core.exceptions import TooManyRequestsError
from app.services.storage_service import StorageError
from app.services.submission_service import SubmissionService

LIMIT = 3


@pytest.fixture(autouse=True)
def _small_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SUBMISSION_RATE_LIMIT", LIMIT)

    async def _refuse(self: SubmissionService, *args: Any, **kwargs: Any) -> None:
        raise StorageError("stubbed")

    monkeypatch.setattr(SubmissionService, "create", _refuse)


def _form() -> dict[str, Any]:
    return {
        "data": {"contributor_name": "Ada", "contributor_email": "ada@example.com"},
        "files": {"photos": ("a.jpg", b"bytes", "image/jpeg")},
    }


async def _submit(client: AsyncClient, ip: str = "203.0.113.7") -> Any:
    return await client.post("/api/v1/submissions/", headers={"X-Forwarded-For": ip}, **_form())


@pytest.mark.asyncio
async def test_the_upload_after_the_budget_is_refused_with_429(async_client: AsyncClient) -> None:
    statuses = [(await _submit(async_client)).status_code for _ in range(LIMIT + 1)]

    assert statuses == [422] * LIMIT + [429]
    refused = await _submit(async_client)
    assert refused.json()["message"] == "Too many uploads. Please wait before submitting again."
    assert int(refused.headers["Retry-After"]) > 0


@pytest.mark.asyncio
async def test_each_address_has_its_own_budget(async_client: AsyncClient) -> None:
    for _ in range(LIMIT):
        await _submit(async_client, ip="203.0.113.7")
    assert (await _submit(async_client, ip="198.51.100.9")).status_code == 422


@pytest.mark.asyncio
async def test_a_signed_in_account_cannot_escape_by_changing_address() -> None:
    user = SimpleNamespace(id=42)

    def _request(ip: str) -> Request:
        return Request({"type": "http", "headers": [(b"x-forwarded-for", ip.encode())]})

    for n in range(LIMIT):
        await submissions._limit_uploads(_request(f"203.0.113.{n}"), user)  # type: ignore[arg-type]
    with pytest.raises(TooManyRequestsError):
        await submissions._limit_uploads(_request("198.51.100.1"), user)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_limit_can_be_switched_off_for_load_tests(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    statuses = {(await _submit(async_client)).status_code for _ in range(LIMIT + 2)}
    assert statuses == {422}
