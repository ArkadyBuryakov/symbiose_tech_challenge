"""Structured JSON logging.

Every log line carries ``service`` and, when available, ``request_id``,
``trace_id``/``span_id`` and the domain identifiers (``job_id``,
``dataset_id``, ``tenant_id``). Context is bound per request/job with
``structlog.contextvars`` so call sites do not have to thread it through.

Nothing secret is ever logged: presigned URLs, cookies, tokens and API keys
must never be passed to a logger.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, unbind_contextvars
from structlog.types import EventDict, Processor

__all__ = [
    "bind_log_context",
    "configure_logging",
    "get_logger",
    "log_context",
]

_NOISY_LOGGERS = (
    "uvicorn.access",  # replaced by our own access log
    "httpx",  # one INFO line per upstream call, with the full URL
    "httpcore",
    "botocore",
    "boto3",
    "urllib3",
    "s3transfer",
)


def _add_otel_context(_logger: Any, _name: str, event_dict: EventDict) -> EventDict:
    """Attach the active trace/span ids so logs and traces can be correlated."""
    from opentelemetry import trace

    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event_dict.setdefault("trace_id", format(ctx.trace_id, "032x"))
        event_dict.setdefault("span_id", format(ctx.span_id, "016x"))
    return event_dict


def configure_logging(
    *,
    service: str,
    level: str = "INFO",
    fmt: str = "json",
    git_sha: str | None = None,
) -> None:
    """Configure structlog + stdlib logging for a service process."""
    level = level.upper()
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_otel_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy, confluent-kafka) through structlog.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    bind_contextvars(service=service)
    if git_sha:
        bind_contextvars(version=git_sha)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger tagged with the calling module.

    ``structlog.stdlib.add_logger_name`` is deliberately not used: the platform
    logs through ``PrintLoggerFactory`` (no stdlib formatting overhead on the
    hot path) and that logger has no ``.name`` attribute. The module is bound as
    an initial value on the *lazy* proxy instead of with ``.bind()``, because
    ``.bind()`` realises the logger immediately — module-level loggers are
    created at import time, before ``configure_logging`` has run.
    """
    logger: structlog.stdlib.BoundLogger = (
        structlog.get_logger(module=name) if name else structlog.get_logger()
    )
    return logger


def bind_log_context(**kwargs: Any) -> None:
    """Bind values onto every subsequent log line in this context."""
    bind_contextvars(**{k: v for k, v in kwargs.items() if v is not None})


@contextmanager
def log_context(**kwargs: Any) -> Iterator[None]:
    """Temporarily bind log context (used per Kafka message / per job)."""
    present = {k: v for k, v in kwargs.items() if v is not None}
    bind_contextvars(**present)
    try:
        yield
    finally:
        unbind_contextvars(*present)
