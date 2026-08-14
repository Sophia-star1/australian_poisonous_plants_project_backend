"""Unit tests for JWT creation and verification.

These are pure-function tests: no database, no HTTP. They pin down the
`type` claim enforcement that stops a long-lived refresh token from being
replayed as an access token.
"""

from app.core.security import (
    ACCESS_TOKEN_TYPE,
    REFRESH_TOKEN_TYPE,
    create_access_token,
    create_refresh_token,
    verify_token,
)

CLAIMS = {"sub": "1", "email": "user@example.com", "role": "user"}


def test_access_token_verifies_as_access() -> None:
    """A freshly minted access token is accepted by the default check."""
    claims = verify_token(create_access_token(CLAIMS))
    assert claims is not None
    assert claims["sub"] == "1"
    assert claims["type"] == ACCESS_TOKEN_TYPE


def test_refresh_token_verifies_as_refresh() -> None:
    """A refresh token is accepted when a refresh token is expected."""
    claims = verify_token(create_refresh_token(CLAIMS), expected_type=REFRESH_TOKEN_TYPE)
    assert claims is not None
    assert claims["type"] == REFRESH_TOKEN_TYPE


def test_refresh_token_rejected_as_access_token() -> None:
    """Regression: a refresh token must not authenticate a normal request.

    Before this check existed, `verify_token` decoded any validly signed JWT,
    so a 7-day refresh token could be sent as a bearer token and reach every
    protected endpoint, including the admin ones.
    """
    assert verify_token(create_refresh_token(CLAIMS)) is None


def test_access_token_rejected_at_refresh_endpoint() -> None:
    """The inverse also holds: an access token cannot be exchanged."""
    token = create_access_token(CLAIMS)
    assert verify_token(token, expected_type=REFRESH_TOKEN_TYPE) is None


def test_tampered_token_rejected() -> None:
    """A token with a mutated payload fails signature verification."""
    token = create_access_token(CLAIMS)
    head, payload, sig = token.split(".")
    assert verify_token(f"{head}.{payload}x.{sig}") is None


def test_garbage_token_rejected() -> None:
    """Malformed input returns None rather than raising."""
    assert verify_token("not-a-jwt") is None
    assert verify_token("") is None
