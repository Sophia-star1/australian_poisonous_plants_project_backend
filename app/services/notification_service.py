"""Telling a contributor what happened to their submission.

Composition lives here; delivery is `EmailService`'s job.

## Why this takes plain values and not the ORM row

`ReviewOutcome` is a snapshot, built while the request's session is still
open. The notification is sent *after* the response — the session is closed
by then, and reading a mapped attribute on a detached instance raises
`MissingGreenlet` under asyncio rather than quietly loading it. That failure
mode has already cost this codebase one production bug: the review endpoint
returned 500 for every approval because it read an attribute off an instance
whose session had expired it. Copying the handful of fields out while they are
still readable removes the possibility rather than relying on remembering it.

It also keeps the templates testable without a database.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.services.email_service import Email, EmailService

if TYPE_CHECKING:  # pragma: no cover
    from app.models.plant import Plant
    from app.models.submission import Submission

logger = get_logger(__name__)

APPROVED = "approved"
REJECTED = "rejected"

#: How much of the contributor's own description to quote back, so they can
#: tell which submission this is about without it turning into a wall of text.
_DESCRIPTION_LIMIT = 140


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """A detached snapshot of a finished review."""

    contributor_name: str
    contributor_email: str
    status: str
    submitted_at: datetime
    description: str | None = None
    reviewer_notes: str | None = None
    botanical_name: str | None = None

    @classmethod
    def from_review(cls, submission: Submission, plant: Plant | None = None) -> ReviewOutcome:
        """Copy what the email needs while the session can still be read."""
        return cls(
            contributor_name=submission.contributor_name,
            contributor_email=submission.contributor_email,
            status=submission.status,
            submitted_at=submission.submitted_at,
            description=submission.user_description,
            reviewer_notes=submission.reviewer_notes,
            botanical_name=plant.botanical_name if plant is not None else None,
        )


def _first_name(full: str) -> str:
    """A greeting, not an identity check — a blank name just drops it."""
    return (full or "").strip().split(" ")[0]


def _shorten(text: str | None) -> str | None:
    if not text:
        return None
    trimmed = " ".join(text.split())
    if len(trimmed) <= _DESCRIPTION_LIMIT:
        return trimmed
    return trimmed[: _DESCRIPTION_LIMIT - 1].rstrip() + "…"


def build_email(outcome: ReviewOutcome) -> Email | None:
    """Compose the message for a finished review, or `None` for no message.

    Only approvals and rejections produce mail. A submission moving to any
    other state is not a decision the contributor is waiting on.
    """
    if outcome.status not in (APPROVED, REJECTED):
        return None

    greeting = _first_name(outcome.contributor_name)
    hello = f"Hi {greeting}," if greeting else "Hello,"
    submitted = outcome.submitted_at.strftime("%d %B %Y")
    description = _shorten(outcome.description)

    lines: list[str] = [hello, ""]
    if outcome.status == APPROVED:
        subject = "Your plant submission has been approved"
        lines.append(
            "Thank you — the observation you submitted has been reviewed and added "
            "to the Australian Poisonous Plants database."
        )
        if outcome.botanical_name:
            lines += ["", f"It was identified as {outcome.botanical_name}."]
    else:
        subject = "Your plant submission was not added"
        lines.append(
            "Thank you for taking the time to submit an observation. After review "
            "it has not been added to the database."
        )

    lines += ["", f"Submitted on {submitted}"]
    if description:
        lines.append(f"Your description: {description}")
    if outcome.reviewer_notes:
        lines += ["", f"Reviewer's notes: {outcome.reviewer_notes.strip()}"]

    if outcome.status == REJECTED:
        lines += [
            "",
            "You are welcome to submit again — clear, well-lit photographs of the "
            "leaves, flowers or fruit are the most useful.",
        ]

    lines += ["", "— Australian Poisonous Plants"]
    text = "\n".join(lines)

    return Email(to=outcome.contributor_email, subject=subject, text=text, html=_as_html(lines))


def _as_html(lines: list[str]) -> str:
    """Wrap the same words in minimal HTML.

    Every line is escaped. The description, the reviewer's notes and the
    contributor's own name all originate from user input, and a submission
    described as `<script>…` would otherwise arrive as markup rather than as
    the text somebody typed.
    """
    paragraphs = [
        f"<p style=\"margin:0 0 12px\">{html.escape(line)}</p>" for line in lines if line
    ]
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        'font-size:15px;line-height:1.55;color:#1f2933">'
        + "".join(paragraphs)
        + "</div>"
    )


async def notify_review(
    outcome: ReviewOutcome, service: EmailService | None = None
) -> bool:
    """Send the review notification. Returns whether it was delivered.

    Runs after the response and after the transaction has committed, so a
    failure here cannot undo the review — and there is nothing to undo it for.
    Failures are logged by `EmailService`; this returns the outcome for tests
    and for any future caller that wants to record it.
    """
    email = build_email(outcome)
    if email is None:
        logger.info("review_notification_skipped", status=outcome.status)
        return False
    return await (service or EmailService()).send(email)
