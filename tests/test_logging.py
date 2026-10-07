"""Tests for what actually reaches Cloud Logging.

Every line used to render as the literal string `null`: a stub processor with
a body of `...` returned None and the JSON renderer serialised that. Nothing
noticed because no test rendered a line through the production chain — the
test client never runs the lifespan that configured it.

These render through `PROCESSORS` itself, into a buffer, without touching
structlog's global configuration.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
import structlog
from httpx import AsyncClient

from app.core.logging import PROCESSORS


def _render(emit: Any) -> list[dict[str, Any]]:
    """Run `emit(logger)` against the production chain and parse each line."""
    buffer = io.StringIO()
    logger = structlog.wrap_logger(structlog.PrintLogger(file=buffer), processors=PROCESSORS)
    emit(logger)
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


def test_a_line_is_a_json_object_not_null() -> None:
    lines = _render(lambda log: log.info("request_completed", status_code=200))
    assert lines and lines[0] is not None
    assert lines[0]["event"] == "request_completed"
    assert lines[0]["status_code"] == 200


@pytest.mark.parametrize(
    ("method", "severity"),
    [("info", "INFO"), ("warning", "WARNING"), ("error", "ERROR")],
)
def test_cloud_logging_severity_is_set(method: str, severity: str) -> None:
    """Without `severity` every entry lands at DEFAULT and errors do not stand out."""
    (line,) = _render(lambda log: getattr(log, method)("something"))
    assert line["severity"] == severity


def test_an_exception_carries_its_traceback() -> None:
    """`logger.exception` is how a 500 is recorded; the traceback is the point."""

    def emit(log: Any) -> None:
        try:
            raise ZeroDivisionError("boom")
        except ZeroDivisionError:
            log.exception("unhandled_exception")

    (line,) = _render(emit)
    assert "ZeroDivisionError: boom" in line["exception"]
    assert line["severity"] == "ERROR"


def test_the_request_id_reaches_lines_logged_inside_a_request() -> None:
    """Bound once by the middleware, merged into every line from then on."""
    structlog.contextvars.bind_contextvars(request_id="req-123")
    try:
        (line,) = _render(lambda log: log.error("storage_copy_failed"))
    finally:
        structlog.contextvars.clear_contextvars()
    assert line["request_id"] == "req-123"


@pytest.mark.asyncio
async def test_the_middleware_binds_the_id_it_returns(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The id in the X-Request-ID header is the one bound for the request's logs."""
    seen: dict[str, Any] = {}

    from app.middleware import logging_middleware

    def _spy(**values: Any) -> None:
        seen.update(values)

    monkeypatch.setattr(logging_middleware.structlog.contextvars, "bind_contextvars", _spy)
    response = await async_client.get("/health")
    assert seen["request_id"] == response.headers["X-Request-ID"]
