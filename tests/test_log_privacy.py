"""Email addresses are masked in logs.

Logs are kept by the hosting platform and readable by whoever operates it,
so a recipient's full address there is personal data held for no purpose.
"""

from __future__ import annotations

import pytest
import structlog

from app.config import settings
from app.core.logging import mask_email
from app.services.email_service import Email, EmailService


@pytest.mark.parametrize(
    ("address", "masked"),
    [
        ("ada@example.org", "a***@example.org"),
        ("  Bob.Smith@uni.edu.au ", "B***@uni.edu.au"),
        ("x@y.z", "x***@y.z"),
        ("not-an-address", "***"),
        ("", "***"),
    ],
)
def test_an_address_keeps_only_its_first_letter_and_domain(address: str, masked: str) -> None:
    assert mask_email(address) == masked


@pytest.mark.asyncio
async def test_an_unsent_message_is_logged_without_the_full_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    monkeypatch.setattr(settings, "RESEND_API_KEY", "")

    with structlog.testing.capture_logs() as logs:
        await EmailService().send(Email(to="ada.lovelace@example.org", subject="s", text="t"))

    entry = next(e for e in logs if e["event"] == "email_not_sent")
    assert entry["to"] == "a***@example.org"
    assert "ada.lovelace" not in repr(logs)
