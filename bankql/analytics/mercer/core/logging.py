"""Structured logging setup via structlog.

- Development (LOG_LEVEL=DEBUG or interactive TTY): ColourConsoleRenderer
- Production: JSON renderer, one object per line
- Call get_logger(name) anywhere in the codebase.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog


_configured: bool = False


def configure_logging() -> None:
    """Idempotent: safe to call multiple times."""
    global _configured
    if _configured:
        return

    log_level = os.environ.get("BANKQL_MERCER_LOG_LEVEL", "WARNING").upper()  # bankql patch: env, not settings
    level_int = getattr(logging, log_level, logging.WARNING)

    # Wire stdlib logging so third-party libraries respect our level
    logging.getLogger("bankql.mercer").setLevel(level_int)  # bankql patch: don't reconfigure the host app's root logger

    is_dev = log_level == "DEBUG"  # bankql patch: never colour by TTY
    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer(colors=True)
        if is_dev
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.ExceptionRenderer(),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),  # bankql patch: stderr (stdout is MCP stdio)
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.types.FilteringBoundLogger:
    """Return a configured structlog logger for the given module name."""
    configure_logging()
    return structlog.get_logger(name)  # type: ignore[no-any-return]
