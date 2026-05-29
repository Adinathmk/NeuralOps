"""
app/core/logging.py

Structured JSON logging configuration using structlog.

Every log line emits JSON with at minimum:
  timestamp, level, service, event, request_id, tenant_id, trace_id

The request_id and tenant_id are injected by middleware via contextvars
so they appear automatically on every log line within a request scope.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Optional

import structlog

from app.core.config import get_settings

# ── Context variables (populated by middleware) ───────────────────────────────
# These propagate automatically into every structlog log call made within
# the same async task / request context.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
tenant_id_ctx: ContextVar[str] = ContextVar("tenant_id", default="-")
trace_id_ctx: ContextVar[str] = ContextVar("trace_id", default="-")


def _add_service_context(
    logger: logging.Logger,
    method: str,
    event_dict: dict,
) -> dict:
    """Structlog processor: inject service name and context vars."""
    settings = get_settings()
    event_dict["service"] = settings.APP_NAME
    event_dict["version"] = settings.APP_VERSION
    event_dict["request_id"] = request_id_ctx.get()
    event_dict["tenant_id"] = tenant_id_ctx.get()
    event_dict["trace_id"] = trace_id_ctx.get()
    return event_dict


def configure_logging() -> None:
    """
    Configure structlog and the stdlib root logger.

    Call once at application startup (inside the lifespan handler).
    """
    settings = get_settings()

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        _add_service_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.is_production or not settings.DEBUG:
        # JSON output for production / log aggregation
        renderer = structlog.processors.JSONRenderer()
    else:
        # Colourful console output for local development
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if settings.DEBUG else logging.INFO
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)

    # Quieten noisy third-party loggers
    noisy_loggers = [
        "uvicorn.access",
        "sqlalchemy.engine",
        "asyncpg",
        "aiokafka",
        "aiokafka.conn",
        "aiokafka.consumer.group_coordinator",
        "aiokafka.consumer.fetcher",
    ]
    for noisy in noisy_loggers:
        logging.getLogger(noisy).setLevel(
            logging.WARNING if not settings.DEBUG else logging.INFO
        )


def get_logger(name: str = __name__) -> structlog.BoundLogger:
    """Return a structlog BoundLogger scoped to the given name."""
    return structlog.get_logger(name)
