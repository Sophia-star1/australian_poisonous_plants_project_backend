"""The /docs "Authorize" dialog has to be usable.

Auth was declared as an OAuth2 password flow pointing at /auth/login, so the
dialog posted the credentials as a form to an endpoint that takes JSON, and
signing in there always failed with 422. It is plain HTTP bearer auth now: the
dialog asks for the token, which /auth/login returns.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_the_docs_ask_for_a_bearer_token(async_client: AsyncClient) -> None:
    schemes = (await async_client.get("/openapi.json")).json()["components"]["securitySchemes"]
    assert list(schemes.values()) == [
        {
            "type": "http",
            "scheme": "bearer",
            "description": "The access_token returned by POST /api/v1/auth/login.",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "message"),
    [
        ({}, "Authentication required."),
        ({"Authorization": "Basic dXNlcjpwYXNz"}, "Authentication required."),
        ({"Authorization": "Bearer not-a-real-token"}, "Invalid or expired token."),
    ],
    ids=["no header", "not bearer", "bad token"],
)
async def test_a_protected_route_still_refuses_without_a_valid_token(
    async_client: AsyncClient, header: dict[str, str], message: str
) -> None:
    response = await async_client.get("/api/v1/auth/me", headers=header)
    assert response.status_code == 401
    assert response.json()["message"] == message
