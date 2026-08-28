"""Outgoing transactional mail, delivered through Resend.

Only the transport lives here — what to say and when to say it belongs to
whatever is doing the notifying. Resend's API is a single JSON POST, so this
reuses the `httpx` already in the dependency list rather than adding an SDK.

Two rules shape the whole module.

**Sending never raises.** Every caller is notifying somebody about work that
already succeeded. A mail provider that is slow, rate-limiting, or simply down
must not turn a completed review into a failed request — the reviewer would
see an error, retry, and hit the "already reviewed" guard. `send()` returns
whether delivery was accepted and logs the reason when it was not.

**No credentials means log, do not crash.** Without an API key the message is
recorded at INFO and reported as not sent, which keeps a bare checkout and the
test suite working and makes a misconfigured deployment obvious in the logs
rather than silent.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"


@dataclass(frozen=True, slots=True)
class Email:
    """One message, ready to send."""

    to: str
    subject: str
    text: str
    html: str | None = None


class EmailService:
    """Send mail through Resend, or account for it in the log when it cannot."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.enabled = settings.email_enabled
        self._timeout = timeout
        if not self.enabled:
            logger.warning(
                "email_backend_disabled",
                detail=(
                    "RESEND_API_KEY is unset; messages will be logged rather than "
                    "delivered. Set it to notify contributors."
                ),
            )

    def _payload(self, email: Email) -> dict[str, object]:
        body: dict[str, object] = {
            "from": settings.EMAIL_FROM,
            "to": [email.to],
            "subject": email.subject,
            "text": email.text,
        }
        if email.html:
            body["html"] = email.html
        if settings.EMAIL_REPLY_TO:
            body["reply_to"] = settings.EMAIL_REPLY_TO
        return body

    async def send(self, email: Email) -> bool:
        """Deliver one message. Returns whether the provider accepted it.

        Never raises. See the module docstring: the caller has already done
        the work this message is about.
        """
        if not self.enabled:
            logger.info(
                "email_not_sent",
                to=email.to,
                subject=email.subject,
                reason="no API key configured",
            )
            return False

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    RESEND_ENDPOINT,
                    json=self._payload(email),
                    headers={
                        "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            # A timeout or DNS failure. Nothing to retry against here; the
            # review it refers to has already been recorded.
            logger.error(
                "email_send_error",
                to=email.to,
                subject=email.subject,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False

        if response.status_code >= 400:
            logger.error(
                "email_send_failed",
                to=email.to,
                subject=email.subject,
                status=response.status_code,
                body=response.text[:300],
            )
            return False

        message_id = None
        try:
            message_id = response.json().get("id")
        except ValueError:
            pass
        logger.info("email_sent", to=email.to, subject=email.subject, message_id=message_id)
        return True
