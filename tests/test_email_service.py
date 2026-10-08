"""Tests for the SMTP and Resend transports.

The behaviour worth pinning is what happens when things go wrong. Every caller
is telling somebody about work that already succeeded, so a provider outage
must produce a log line and a `False`, never an exception that unwinds back
into a request whose real work is already committed.
"""

from __future__ import annotations

import smtplib
import threading
from email.message import EmailMessage
from typing import Any

import httpx
import pytest

from app.config import Settings, settings
from app.services import email_service
from app.services.email_service import RESEND_ENDPOINT, Email, EmailService


@pytest.fixture(autouse=True)
def _no_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with SMTP off, whatever the local .env says.

    SMTP_HOST decides the transport, so one left over from a developer's .env
    would send the Resend tests down the SMTP path.
    """
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    monkeypatch.setattr(settings, "SMTP_PORT", 587)
    monkeypatch.setattr(settings, "SMTP_USERNAME", "")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(settings, "EMAIL_FROM", "APL <noreply@example.org>")
    monkeypatch.setattr(settings, "EMAIL_REPLY_TO", "")


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Intercept outgoing HTTP so no real request leaves the test."""

    def _apply(
        status: int = 200, body: Any = None, boom: Exception | None = None
    ) -> dict[str, Any]:
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


# ── SMTP ─────────────────────────────────────────────────────────────────────

PASSWORD = "s3cret pass"


class FakeSMTP:
    """Stands in for `smtplib.SMTP` / `SMTP_SSL` and records the conversation.

    `fail` maps a step name to the exception to raise at that step.
    """

    def __init__(
        self, record: dict[str, Any], fail: dict[str, Exception], implicit_tls: bool
    ) -> None:
        self.record = record
        self.fail = fail
        self.implicit_tls = implicit_tls

    def __call__(self, host: str, port: int, timeout: float | None = None, **kw: Any) -> FakeSMTP:
        if "connect" in self.fail:
            raise self.fail["connect"]
        self.record.update(host=host, port=port, timeout=timeout, implicit_tls=self.implicit_tls)
        return self

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.record["steps"].append("quit")

    def starttls(self, context: Any = None) -> None:
        if "starttls" in self.fail:
            raise self.fail["starttls"]
        self.record["steps"].append("starttls")

    def login(self, user: str, password: str) -> None:
        self.record["steps"].append("login")
        self.record["credentials"] = (user, password)
        if "login" in self.fail:
            raise self.fail["login"]

    def send_message(self, message: EmailMessage) -> None:
        self.record["steps"].append("send")
        self.record["message"] = message
        self.record["thread"] = threading.get_ident()
        if "send" in self.fail:
            raise self.fail["send"]


@pytest.fixture
def smtp_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SMTP_HOST", "mail.example.org")
    monkeypatch.setattr(settings, "SMTP_USERNAME", "info@example.org")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", PASSWORD)
    monkeypatch.setattr(settings, "EMAIL_FROM", "APL <info@example.org>")
    monkeypatch.setattr(settings, "EMAIL_REPLY_TO", "")
    # Configured too, to show SMTP wins rather than both being tried.
    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_k")


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace both smtplib classes; returns what the conversation recorded."""

    def _apply(**fail: Exception) -> dict[str, Any]:
        record: dict[str, Any] = {"steps": []}
        monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSMTP(record, fail, False))
        monkeypatch.setattr(email_service.smtplib, "SMTP_SSL", FakeSMTP(record, fail, True))
        return record

    return _apply


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """Record what the service logs, so a failure can be checked for detail."""
    seen: list[tuple[str, str, dict[str, Any]]] = []

    class _Recorder:
        def __getattr__(self, level: str) -> Any:
            return lambda event, **fields: seen.append((level, event, fields))

    monkeypatch.setattr(email_service, "logger", _Recorder())
    return seen


@pytest.mark.asyncio
@pytest.mark.usefixtures("smtp_configured")
async def test_smtp_delivers_over_starttls_instead_of_resend(smtp: Any, capture: Any) -> None:
    record = smtp()
    http = capture()

    assert await EmailService().send(_email()) is True

    assert (record["host"], record["port"], record["implicit_tls"]) == (
        "mail.example.org",
        587,
        False,
    )
    assert record["timeout"] == 10.0
    # STARTTLS before the password crosses the wire, then a clean QUIT.
    assert record["steps"] == ["starttls", "login", "send", "quit"]
    assert record["credentials"] == ("info@example.org", PASSWORD)
    assert http == {}, "with SMTP configured, Resend must not be called"


@pytest.mark.asyncio
@pytest.mark.usefixtures("smtp_configured")
async def test_the_smtp_message_carries_both_bodies_and_the_headers(smtp: Any) -> None:
    record = smtp()
    await EmailService().send(_email())

    message: EmailMessage = record["message"]
    assert message["From"] == "APL <info@example.org>"
    assert message["To"] == "ada@example.com"
    assert message["Subject"] == "Your submission was approved"
    assert message["Date"], "mail without a Date scores worse with spam filters"
    assert message["Message-ID"].endswith("@example.org>")
    assert "Reply-To" not in message
    plain = message.get_body(preferencelist=("plain",))
    html = message.get_body(preferencelist=("html",))
    assert plain is not None and plain.get_content().strip() == "Plain text body."
    assert html is not None and html.get_content().strip() == "<p>Plain text body.</p>"


@pytest.mark.asyncio
@pytest.mark.usefixtures("smtp_configured")
async def test_smtp_text_only_has_no_html_part(smtp: Any) -> None:
    record = smtp()
    await EmailService().send(Email(to="a@b.com", subject="s", text="body only"))
    message: EmailMessage = record["message"]
    assert not message.is_multipart()
    assert message.get_content().strip() == "body only"


@pytest.mark.asyncio
@pytest.mark.usefixtures("smtp_configured")
async def test_smtp_reply_to_is_set_when_configured(
    smtp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "EMAIL_REPLY_TO", "team@example.org")
    record = smtp()
    await EmailService().send(_email())
    assert record["message"]["Reply-To"] == "team@example.org"


@pytest.mark.asyncio
@pytest.mark.usefixtures("smtp_configured")
async def test_port_465_uses_implicit_tls_without_starttls(
    smtp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "SMTP_PORT", 465)
    record = smtp()
    assert await EmailService().send(_email()) is True
    assert record["implicit_tls"] is True
    assert record["steps"] == ["login", "send", "quit"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("smtp_configured")
async def test_smtp_runs_off_the_event_loop(smtp: Any) -> None:
    """smtplib blocks for the whole conversation; inline it stalls every request."""
    record = smtp()
    await EmailService().send(_email())
    assert record["thread"] != threading.get_ident()


@pytest.mark.asyncio
@pytest.mark.usefixtures("smtp_configured")
async def test_a_wrong_password_reports_false_with_the_server_reply(
    smtp: Any, logs: list[Any]
) -> None:
    """The server's own words name the problem; the password must not appear."""
    smtp(login=smtplib.SMTPAuthenticationError(535, b"5.7.8 Error: authentication failed"))

    assert await EmailService().send(_email()) is False

    ((level, event, fields),) = logs
    assert (level, event, fields["status"]) == ("error", "email_send_failed", 535)
    assert "authentication failed" in fields["body"]
    assert PASSWORD not in repr(logs)


@pytest.mark.asyncio
@pytest.mark.usefixtures("smtp_configured")
async def test_an_unreachable_server_reports_false(smtp: Any, logs: list[Any]) -> None:
    smtp(connect=TimeoutError("timed out"))
    assert await EmailService().send(_email()) is False
    assert [event for _, event, _ in logs] == ["email_send_error"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("smtp_configured")
async def test_a_refused_recipient_reports_false(smtp: Any) -> None:
    smtp(send=smtplib.SMTPRecipientsRefused({"ada@example.com": (550, b"no such user")}))
    assert await EmailService().send(_email()) is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("smtp_configured")
async def test_a_server_without_starttls_never_sees_the_password(smtp: Any) -> None:
    """starttls() raising stops the conversation before the login."""
    record = smtp(
        starttls=smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")
    )
    assert await EmailService().send(_email()) is False
    assert "login" not in record["steps"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("smtp_configured")
async def test_smtp_without_a_password_is_disabled_not_resend(
    smtp: Any, capture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing secret must not fall back to Resend with an unverified sender."""
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "")
    record = smtp()
    http = capture()

    service = EmailService()
    assert (service.enabled, service.transport) == (False, "smtp")
    assert await service.send(_email()) is False
    assert record["steps"] == [] and http == {}


def test_the_password_loses_a_trailing_newline_but_keeps_its_spaces() -> None:
    """Secret Manager tends to keep the newline; a space may be deliberate."""
    loaded = Settings(SMTP_PASSWORD=" pass word \n", SMTP_HOST=" mail.example.org\n")
    assert loaded.SMTP_PASSWORD == " pass word "
    assert loaded.SMTP_HOST == "mail.example.org"
