"""What a 422 says to the client, compared whole rather than by substring.

Every other test of a 422 checked `"unsupported type" in message`, which an
`UnprocessableError` passed while its message read `"422: notes.txt: ..."`:
a handler registered for the bare 422 status took precedence over the
`AppException` one and rendered the exception with `str()`. The mobile app
shows that message to contributors, prefix and all.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.config import settings


@pytest.mark.asyncio
async def test_an_unprocessable_error_message_is_its_detail_alone(
    async_client: AsyncClient,
) -> None:
    """Refused before anything is stored or screened, so no stubs are needed."""
    response = await async_client.post(
        "/api/v1/submissions/",
        data={"contributor_name": "Ada", "contributor_email": "ada@example.com"},
        files={"photos": ("notes.txt", b"not an image", "text/plain")},
    )

    allowed = ", ".join(settings.ALLOWED_IMAGE_TYPES)
    assert response.status_code == 422
    assert response.json() == {
        "code": 422,
        "message": f"notes.txt: unsupported type 'text/plain'. Allowed: {allowed}.",
        "data": None,
    }


@pytest.mark.asyncio
async def test_a_request_validation_error_keeps_the_envelope(
    async_client: AsyncClient,
) -> None:
    """FastAPI's own 422 still reads as one sentence, not a `detail` array."""
    response = await async_client.post("/api/v1/auth/signup", json={"email": "ada@example.com"})

    assert response.status_code == 422
    assert response.json() == {"code": 422, "message": "password: Field required", "data": None}
