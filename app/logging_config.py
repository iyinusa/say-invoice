from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure_logging(env: str) -> None:
    is_prod = env.lower() == "production"

    from app.security.redact import AccessLogFilter, structlog_processor

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog_processor,  # no emails, phone numbers or client-link tokens in logs
    ]

    renderer: Any
    if is_prod:
        renderer = structlog.processors.JSONRenderer()
    else:
        # No ANSI codes when piped (docker logs, Render): only colour a real terminal.
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout, force=True)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogFilter) for f in access.filters):
        access.addFilter(AccessLogFilter())


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
