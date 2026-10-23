"""Outgoing transactional mail, delivered over SMTP or through Resend.

Only the transport lives here — what to say and when to say it belongs to
whatever is doing the notifying.

Two transports, chosen by configuration (`settings.email_transport`):

* **SMTP** when `SMTP_HOST` is set — the domain's own mailbox, e.g. Namecheap
  Private Email. Python's `smtplib`, so no new dependency; it blocks, so it
  runs in a worker thread rather than on the event loop.
* **Resend** otherwise. Its API is a single JSON POST, so this reuses the
  `httpx` already in the dependency list rather than adding an SDK.

Two rules shape the whole module.

**Sending never raises.** Every caller is notifying somebody about work that
already succeeded. A mail provider that is slow, rate-limiting, or simply down
must not turn a completed review into a failed request — the reviewer would
see an error, retry, and hit the "already reviewed" guard. `send()` returns
whether delivery was accepted and logs the reason when it was not.

**No credentials means log, do not crash.** Without them the message is
recorded at INFO and reported as not sent, which keeps a bare checkout and the
test suite working and makes a misconfigured deployment obvious in the logs
rather than silent.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from contextlib import suppress
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

import httpx

from app.config import settings
from app.core.logging import get_logger, mask_email

logger = get_logger(__name__)

RESEND_ENDPOINT = "https://api.resend.com/emails"

#: The port where TLS starts with the connection rather than after STARTTLS.
SMTPS_PORT = 465


@dataclass(frozen=True, slots=True)
class Email:
    """One message, ready to send."""

    to: str
    subject: str
    text: str
    html: str | None = None


class EmailService:
    """Send mail, or account for it in the log when it cannot."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.enabled = settings.email_enabled
        self.transport = settings.email_transport
        self._timeout = timeout
        if not self.enabled:
            missing = (
                "SMTP_USERNAME / SMTP_PASSWORD (SMTP_HOST is set)"
                if self.transport == "smtp"
                else "RESEND_API_KEY or SMTP_HOST"
            )
            logger.warning(
                "email_backend_disabled",
                detail=(
                    f"{missing} is unset, or EMAIL_FROM is empty; messages will be "
                    "logged rather than delivered."
                ),
            )

    async def send(self, email: Email) -> bool:
        """Deliver one message. Returns whether the provider accepted it.

        Never raises. See the module docstring: the caller has already done
        the work this message is about.
        """
        if not self.enabled:
            logger.info(
                "email_not_sent",
                to=mask_email(email.to),
                subject=email.subject,
                reason="no mail credentials configured",
            )
            return False
        if self.transport == "smtp":
            return await self._send_smtp(email)
        return await self._send_resend(email)

    # ── SMTP ─────────────────────────────────────────────────────────────────

    def _message(self, email: Email) -> EmailMessage:
        """The MIME message: plain text always, HTML as the alternative.

        `Date` and `Message-ID` are set here because nothing downstream adds
        them reliably, and mail missing either scores worse with spam filters.
        """
        message = EmailMessage()
        message["From"] = settings.EMAIL_FROM
        message["To"] = email.to
        message["Subject"] = email.subject
        message["Date"] = formatdate(usegmt=True)
        sender_domain = parseaddr(settings.EMAIL_FROM)[1].rpartition("@")[2] or None
        message["Message-ID"] = make_msgid(domain=sender_domain)
        if settings.EMAIL_REPLY_TO:
            message["Reply-To"] = settings.EMAIL_REPLY_TO
        message.set_content(email.text)
        if email.html:
            message.add_alternative(email.html, subtype="html")
        return message

    def _deliver_smtp(self, message: EmailMessage) -> None:
        """Connect, secure, log in and hand the message over. Blocking.

        TLS is always on: 465 starts with it, anything else upgrades with
        STARTTLS before the password is sent. `starttls()` raises if the server
        does not offer it, so credentials never cross the wire in clear.
        """
        context = ssl.create_default_context()
        client: smtplib.SMTP
        if settings.SMTP_PORT == SMTPS_PORT:
            client = smtplib.SMTP_SSL(
                settings.SMTP_HOST, settings.SMTP_PORT, timeout=self._timeout, context=context
            )
        else:
            client = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=self._timeout)
        with client:
            if settings.SMTP_PORT != SMTPS_PORT:
                client.starttls(context=context)
            client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            client.send_message(message)

    async def _send_smtp(self, email: Email) -> bool:
        message = self._message(email)
        try:
            # smtplib blocks for the whole conversation, a second or more.
            # Inline it would stall every other request this instance serves.
            await asyncio.to_thread(self._deliver_smtp, message)
        except smtplib.SMTPResponseException as exc:
            # The server answered and said no: a wrong password (535), a
            # sender the mailbox may not use, or a rate limit. Logged with the
            # server's own words, which name the actual problem.
            logger.error(
                "email_send_failed",
                transport="smtp",
                to=mask_email(email.to),
                subject=email.subject,
                status=exc.smtp_code,
                body=_decode(exc.smtp_error)[:300],
            )
            return False
        except (smtplib.SMTPException, OSError) as exc:
            # Refused recipients, no STARTTLS, a timeout, DNS or a TLS failure.
            logger.error(
                "email_send_error",
                transport="smtp",
                to=mask_email(email.to),
                subject=email.subject,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False

        logger.info(
            "email_sent",
            transport="smtp",
            to=mask_email(email.to),
            subject=email.subject,
            message_id=message["Message-ID"],
        )
        return True

    # ── Resend ───────────────────────────────────────────────────────────────

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

    async def _send_resend(self, email: Email) -> bool:
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
                transport="resend",
                to=mask_email(email.to),
                subject=email.subject,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False

        if response.status_code >= 400:
            logger.error(
                "email_send_failed",
                transport="resend",
                to=mask_email(email.to),
                subject=email.subject,
                status=response.status_code,
                body=response.text[:300],
            )
            return False

        # Delivery was accepted either way; only the id for the log is missing
        # if the body is not the JSON the provider documents.
        message_id = None
        with suppress(ValueError):
            message_id = response.json().get("id")
        logger.info(
            "email_sent",
            transport="resend",
            to=mask_email(email.to),
            subject=email.subject,
            message_id=message_id,
        )
        return True


def _decode(value: bytes | str) -> str:
    """SMTP replies arrive as bytes; the log wants text."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
