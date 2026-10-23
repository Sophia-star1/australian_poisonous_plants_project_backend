"""Structured logging configuration with structlog.

Every line is one JSON object on stdout, which Cloud Run hands to Cloud
Logging as a structured `jsonPayload`. `severity` is set alongside `level`
because that is the key Cloud Logging reads to file an entry as ERROR rather
than DEFAULT — without it every line lands at the same level and an error is
indistinguishable from a request log.

The request id reaches every line through `structlog.contextvars`: the
request middleware binds it, `merge_contextvars` copies it into each event.
"""

import logging
from collections.abc import MutableMapping
from typing import Any

import structlog


def add_severity(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Copy the level into `severity`, upper-cased, for Cloud Logging."""
    level = event_dict.get("level", method_name)
    event_dict["severity"] = "WARNING" if level == "warn" else str(level).upper()
    return event_dict


#: The processor chain, kept as a module constant so tests can render through
#: exactly what production uses without reconfiguring structlog globally.
#:
#: A processor must return the event dict. One that returned None — this
#: chain used to include a stub `add_request_id` whose body was `...` — hands
#: None to the renderer, and every line in production came out as the literal
#: string `null`: request logs, storage failures, email failures and the
#: traceback of every 500, all of it.
PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    add_severity,
    structlog.processors.TimeStamper(fmt="iso"),
    # `logger.exception(...)` sets exc_info; without this the renderer writes
    # `"exc_info": true` and drops the traceback itself.
    structlog.processors.format_exc_info,
    structlog.processors.JSONRenderer(),
]


def setup_logging() -> None:
    """Initialize structlog with JSON rendering and processors.

    Called from `create_app`, before anything logs. Loggers are cached on
    first use, so a module that logged before this ran (the CORS setup does,
    at import) would keep structlog's default console renderer for the life
    of the process.
    """
    structlog.configure(
        processors=PROCESSORS,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a configured structlog logger instance."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def mask_email(address: str) -> str:
    """`a***@example.org`: enough to tell messages apart in a log, not to contact anyone.

    Logs are kept by the hosting platform and read by whoever operates it, so
    a full address there is personal data held for no purpose. The domain is
    kept because it is what diagnoses delivery problems.
    """
    local, at, domain = address.strip().partition("@")
    if not at:
        return "***"
    return f"{local[:1]}***@{domain}"
