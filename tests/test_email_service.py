"""Tests for the Resend transport.

The behaviour worth pinning is what happens when things go wrong. Every caller
is telling somebody about work that already succeeded, so a provider outage
must produce a log line and a `False`, never an exception that unwinds back
into a request whose real work is already committed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config import settings
from app.services.email_service import RESEND_ENDPOINT, Email, EmailService


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(settings, "EMAIL_FROM", "APL <noreply@example.org>")
    monkeypatch.setattr(settings, "EMAIL_REPLY_TO", "")


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Intercept outgoing HTTP so no real request leaves the test."""

    def _apply(status: int = 200, body: Any = None, boom: Exception | None = None) -> dict[str, Any]:
        recorder: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if boom is not None:
                raise boom
            recorder["url"] = str(request.url)
            recorder["headers"] = dict(request.headers)
            recorder["json"] = __import__("json").loads(request.content)
            return httpx.Response(status, json=body if body is not None else {"id": "msg_1"})

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient.__init__

        def patched(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = transport
            original(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", patched)
        return recorder

    return _apply


def _email() -> Email:
    return Email(
        to="ada@example.com",
        subject="Your submission was approved",
        text="Plain text body.",
        html="<p>Plain text body.</p>",
    )


# ── the happy path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_a_message_reaches_the_provider(capture: Any) -> None:
    recorder = capture()
    assert await EmailService().send(_email()) is True

    assert recorder["url"] == RESEND_ENDPOINT
    assert recorder["headers"]["authorization"] == "Bearer re_test_key"
    body = recorder["json"]
    assert body["to"] == ["ada@example.com"]
    assert body["from"] == "APL <noreply@example.org>"
    assert body["subject"] == "Your submission was approved"
    assert body["text"] == "Plain text body."
    assert body["html"] == "<p>Plain text body.</p>"


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_plain_text_is_always_sent(capture: Any) -> None:
    """Some clients render text only, and some people prefer it that way."""
    recorder = capture()
    await EmailService().send(Email(to="a@b.com", subject="s", text="body only"))
    assert recorder["json"]["text"] == "body only"
    assert "html" not in recorder["json"]


@pytest.mark.asyncio
async def test_reply_to_is_included_when_set(capture: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_k")
    monkeypatch.setattr(settings, "EMAIL_FROM", "APL <noreply@example.org>")
    monkeypatch.setattr(settings, "EMAIL_REPLY_TO", "team@example.org")
    recorder = capture()
    await EmailService().send(_email())
    assert recorder["json"]["reply_to"] == "team@example.org"


# ── failure never escapes ────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_a_rejected_message_reports_false_rather_than_raising(capture: Any) -> None:
    """The caller has already committed the work this message describes.

    Raising would unwind into a request that succeeded, the reviewer would see
    an error and retry, and the retry would hit the already-reviewed guard.
    """
    capture(status=422, body={"message": "invalid to address"})
    assert await EmailService().send(_email()) is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_provider_being_down_reports_false(capture: Any) -> None:
    capture(status=503)
    assert await EmailService().send(_email()) is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_a_network_failure_reports_false(capture: Any) -> None:
    """Timeouts and DNS failures raise below the response, not through it."""
    capture(boom=httpx.ConnectTimeout("timed out"))
    assert await EmailService().send(_email()) is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured")
async def test_an_unparseable_success_body_is_still_a_success(capture: Any) -> None:
    """Delivery was accepted; only the message id is missing."""
    capture(status=200, body=None)
    assert await EmailService().send(_email()) is True


# ── no credentials ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_without_a_key_nothing_is_sent_and_nothing_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkout with no credentials must still run."""
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    service = EmailService()
    assert service.enabled is False
    assert await service.send(_email()) is False


@pytest.mark.asyncio
async def test_a_blank_sender_also_disables_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resend rejects an empty From, so treat it as unconfigured."""
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_k")
    monkeypatch.setattr(settings, "EMAIL_FROM", "")
    assert EmailService().enabled is False


@pytest.mark.asyncio
async def test_the_disabled_path_makes_no_request(
    capture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")
    recorder = capture()
    await EmailService().send(_email())
    assert recorder == {}, "a disabled service must not call the provider"
