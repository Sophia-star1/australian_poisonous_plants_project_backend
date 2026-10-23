"""Tests for the review-outcome email: what it says and what it refuses to say.

The snapshot matters as much as the wording. `ReviewOutcome` exists so the
message can be built from plain values rather than from an ORM row that will
be detached by the time the notification is sent — the same class of bug that
once made every approval return 500.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.email_service import Email
from app.services.notification_service import (
    ReviewOutcome,
    build_email,
    notify_review,
)


def _outcome(**overrides: Any) -> ReviewOutcome:
    base: dict[str, Any] = {
        "contributor_name": "Ada Lovelace",
        "contributor_email": "ada@example.com",
        "status": "approved",
        "submitted_at": datetime(2026, 3, 14, tzinfo=UTC),
        "description": "Growing beside the creek, red seeds.",
        "reviewer_notes": None,
        "botanical_name": "Abrus precatorius",
    }
    base.update(overrides)
    return ReviewOutcome(**base)


# ── what the contributor is told ─────────────────────────────────────────────


def test_an_approval_says_so_and_names_the_plant() -> None:
    email = build_email(_outcome())
    assert email is not None
    assert email.subject == "Your plant submission has been approved"
    assert "added to the Australian Poisonous Plants database" in email.text
    assert "Abrus precatorius" in email.text


def test_a_rejection_does_not_pretend_otherwise() -> None:
    email = build_email(_outcome(status="rejected", botanical_name=None))
    assert email is not None
    assert email.subject == "Your plant submission was not added"
    assert "has not been added" in email.text
    assert "approved" not in email.subject


def test_a_rejection_invites_another_attempt() -> None:
    """A rejection with no way forward reads as a door closing."""
    email = build_email(_outcome(status="rejected", botanical_name=None))
    assert email is not None
    assert "welcome to submit again" in email.text


def test_reviewer_notes_are_passed_on() -> None:
    """The reviewer's reason is the most useful thing in the message."""
    email = build_email(_outcome(status="rejected", reviewer_notes="Photo is too blurred."))
    assert email is not None
    assert "Photo is too blurred." in email.text


def test_the_submission_is_identifiable() -> None:
    """A contributor with several submissions needs to know which one."""
    email = build_email(_outcome())
    assert email is not None
    assert "14 March 2026" in email.text
    assert "Growing beside the creek" in email.text


def test_a_long_description_is_trimmed() -> None:
    """It is a reminder of which submission, not a reproduction of it."""
    email = build_email(_outcome(description="word " * 200))
    assert email is not None
    quoted = [line for line in email.text.splitlines() if line.startswith("Your description:")][0]
    assert len(quoted) < 200
    assert quoted.endswith("…")


def test_a_missing_name_does_not_produce_an_empty_greeting() -> None:
    """`Hi ,` reads worse than no name at all."""
    email = build_email(_outcome(contributor_name="   "))
    assert email is not None
    assert email.text.startswith("Hello,")
    assert "Hi ," not in email.text


def test_an_approval_without_an_identification_still_sends() -> None:
    """`botanical_name` is optional everywhere else; it is optional here too."""
    email = build_email(_outcome(botanical_name=None))
    assert email is not None
    assert "identified as" not in email.text


def test_only_a_decision_produces_a_message() -> None:
    """Nothing has happened that the contributor is waiting on."""
    assert build_email(_outcome(status="pending")) is None


def test_the_message_goes_to_the_submission_address() -> None:
    email = build_email(_outcome(contributor_email="someone@example.net"))
    assert email is not None
    assert email.to == "someone@example.net"


# ── user input is not markup ─────────────────────────────────────────────────


def test_user_text_cannot_inject_html() -> None:
    """Descriptions, notes and names are all typed by people.

    A submission described as `<script>` must arrive as those characters, not
    as a tag — otherwise the contributor's own words become markup in a
    message the project sent.
    """
    email = build_email(
        _outcome(
            contributor_name="<b>Ada</b>",
            description="<script>alert(1)</script>",
            reviewer_notes="<img src=x onerror=alert(2)>",
        )
    )
    assert email is not None and email.html is not None
    # The words may survive; the angle brackets that would make them markup
    # must not. `onerror` inside escaped text is inert, so asserting on the
    # word rather than the syntax would only prove the wrong thing.
    assert "<script>" not in email.html
    assert "<img" not in email.html
    assert "<b>Ada</b>" not in email.html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in email.html
    assert "&lt;img src=x onerror=alert(2)&gt;" in email.html

    # Only the wrapper's own markup is left.
    import re

    tags = set(re.findall(r"</?([a-zA-Z]+)", email.html))
    assert tags <= {"div", "p"}, f"unexpected tags reached the message: {tags}"


def test_the_plain_text_part_carries_the_same_words() -> None:
    """Some clients render text only, and some readers prefer it."""
    email = build_email(_outcome(reviewer_notes="Looks right."))
    assert email is not None and email.html is not None
    assert "Looks right." in email.text
    assert "Looks right." in email.html


# ── dispatch ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notify_sends_the_composed_message() -> None:
    sent: list[Email] = []

    class _Stub:
        async def send(self, email: Email) -> bool:
            sent.append(email)
            return True

    assert await notify_review(_outcome(), service=_Stub()) is True  # type: ignore[arg-type]
    assert sent[0].subject == "Your plant submission has been approved"


@pytest.mark.asyncio
async def test_nothing_is_sent_for_a_non_decision() -> None:
    class _Stub:
        async def send(self, email: Email) -> bool:  # pragma: no cover
            raise AssertionError("should not have been called")

    assert await notify_review(_outcome(status="pending"), service=_Stub()) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_delivery_failure_is_reported_not_raised() -> None:
    """The review it describes is already committed; there is nothing to undo."""

    class _Stub:
        async def send(self, email: Email) -> bool:
            return False

    assert await notify_review(_outcome(), service=_Stub()) is False  # type: ignore[arg-type]


# ── the snapshot ─────────────────────────────────────────────────────────────


def test_the_outcome_is_built_from_plain_values() -> None:
    """Regression guard for the reason this class exists.

    The notification runs after the session closes. Holding an ORM row would
    mean the first attribute read happens on a detached instance, which under
    asyncio raises rather than loading. Everything needed is copied out while
    the row is still live.
    """

    class FakeSubmission:
        contributor_name = "Ada"
        contributor_email = "ada@example.com"
        status = "approved"
        submitted_at = datetime(2026, 1, 1, tzinfo=UTC)
        user_description = "by the creek"
        reviewer_notes = "good photo"

    class FakePlant:
        botanical_name = "Abrus precatorius"

    outcome = ReviewOutcome.from_review(FakeSubmission(), FakePlant())  # type: ignore[arg-type]

    assert outcome.contributor_email == "ada@example.com"
    assert outcome.botanical_name == "Abrus precatorius"
    # Every field is a plain value: nothing here can trigger a database read.
    for value in (
        outcome.contributor_name,
        outcome.contributor_email,
        outcome.status,
        outcome.description,
        outcome.reviewer_notes,
        outcome.botanical_name,
    ):
        assert value is None or isinstance(value, str)
    assert isinstance(outcome.submitted_at, datetime)


def test_a_rejection_carries_no_plant_name() -> None:
    outcome = ReviewOutcome.from_review(
        type(
            "S",
            (),
            {
                "contributor_name": "Ada",
                "contributor_email": "a@b.com",
                "status": "rejected",
                "submitted_at": datetime(2026, 1, 1, tzinfo=UTC),
                "user_description": None,
                "reviewer_notes": None,
            },
        )(),  # type: ignore[arg-type]
        None,
    )
    assert outcome.botanical_name is None
