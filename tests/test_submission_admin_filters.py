"""The admin list's name and email filters match typed text literally.

They were `ILIKE '%<input>%'` with nothing escaped, so `_` and `%` acted as
wildcards: a filter for `first_last@` also matched `firstxlast@`, and a lone
`%` matched every submission. Plant search already escaped them.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.db.like import LIKE_ESCAPE, contains_pattern
from app.schemas.submission import SubmissionFilter
from app.services.submission_service import SubmissionService


@pytest.mark.parametrize(
    ("typed", "pattern"),
    [
        ("ada", "%ada%"),
        ("first_last", "%first\\_last%"),
        ("100%", "%100\\%%"),
        ("back\\slash", "%back\\\\slash%"),
    ],
)
def test_wildcards_in_typed_text_are_escaped(typed: str, pattern: str) -> None:
    assert contains_pattern(typed) == pattern


class _Session:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def scalar(self, statement: Any) -> int:
        self.statements.append(statement)
        return 0

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)

        class _Result:
            def scalars(self) -> Any:
                class _All:
                    def all(self) -> list[Any]:
                        return []

                return _All()

        return _Result()


@pytest.mark.asyncio
async def test_the_admin_filters_send_escaped_patterns() -> None:
    session = _Session()
    filters = SubmissionFilter(contributor_name="a_b", contributor_email="x%y")

    await SubmissionService(session).list_all(filters)  # type: ignore[arg-type]

    for statement in session.statements:  # the count and the page both
        compiled = statement.compile(dialect=postgresql.dialect())
        assert set(compiled.params.values()) >= {"%a\\_b%", "%x\\%y%"}
        assert str(compiled).count(f"ESCAPE '{LIKE_ESCAPE * 2}'") == 2
