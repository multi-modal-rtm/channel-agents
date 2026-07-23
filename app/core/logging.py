"""Structured logging configuration.

Key properties:
- JSON renderer in production, human-readable console in dev
- tenant_id auto-bound to every log line in the request via ContextVar
- Sensitive fields globally scrubbed before any renderer sees them
"""

from __future__ import annotations

import logging
import re
from typing import Any

import structlog

# ── Sensitive field scrubbing ─────────────────────────────────────────────────
# Exact key names that always contain secrets
_SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "api-key",
    "password", "passwd", "pwd",
    "token", "access_token", "refresh_token", "id_token",
    "authorization", "auth",
    "secret", "secret_key", "webhook_secret",
    "anthropic_key", "anthropic_key_encrypted",
    "private_key", "encryption_key",
    "credit_card", "card_number", "cvv",
})

# Regex patterns that match secret-looking values embedded in other keys
_SENSITIVE_PATTERNS = re.compile(
    r"(key|secret|password|token|auth|credential|passwd|api[-_]key)",
    re.IGNORECASE,
)

_REDACTED = "[REDACTED]"


def _scrub_sensitive(
    logger: Any,  # noqa: ARG001
    method: Any,  # noqa: ARG001
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor: replace sensitive values with [REDACTED]."""
    for key in list(event_dict.keys()):
        if key.lower() in _SENSITIVE_KEYS or _SENSITIVE_PATTERNS.search(key):
            event_dict[key] = _REDACTED
    return event_dict


# ── Configuration ─────────────────────────────────────────────────────────────

def configure_logging(environment: str = "development") -> None:
    logging.basicConfig(format="%(message)s", level=logging.DEBUG)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _scrub_sensitive,                           # scrub before any renderer
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
    ]

    if environment == "production":
        processors: list = [
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = [
            *shared_processors,
            structlog.processors.ExceptionRenderer(),
            structlog.dev.ConsoleRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ── Per-request context helpers ───────────────────────────────────────────────

def bind_tenant(tenant_id: str) -> None:
    """Bind tenant_id to all subsequent log lines in this async context."""
    structlog.contextvars.bind_contextvars(tenant_id=tenant_id)


def bind_request(*, method: str, path: str, request_id: str | None = None) -> None:
    structlog.contextvars.bind_contextvars(
        http_method=method,
        http_path=path,
        **({"request_id": request_id} if request_id else {}),
    )


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
