"""Langfuse client singleton — optional observability integration.

Degrades gracefully when LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set.
All calls are wrapped in try/except so a Langfuse outage never affects the
critical path.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

_langfuse = None
_initialised = False


def _get_client():
    global _langfuse, _initialised
    if _initialised:
        return _langfuse
    _initialised = True
    try:
        from app.config import settings
        if settings.langfuse_public_key and settings.langfuse_secret_key:
            from langfuse import Langfuse
            _langfuse = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
            )
    except Exception as exc:
        logger.warning("langfuse_init_failed: %s", exc)
    return _langfuse


def trace_llm_call(
    *,
    tenant_id: UUID,
    agent_type: str | None,
    model: str,
    messages: list[dict[str, Any]],
    response_text: str | None,
    tokens_in: int,
    tokens_out: int,
    latency_ms: int,
    success: bool,
    conversation_id: UUID | None = None,
) -> None:
    """Record an LLM call as a Langfuse generation. Fire-and-forget."""
    lf = _get_client()
    if lf is None:
        return
    try:
        metadata: dict[str, Any] = {
            "tenant_id": str(tenant_id),
            "success": success,
        }
        if agent_type:
            metadata["agent_type"] = agent_type
        if conversation_id:
            metadata["conversation_id"] = str(conversation_id)

        trace = lf.trace(
            name=f"llm.{agent_type or 'unknown'}",
            user_id=str(tenant_id),
            metadata=metadata,
            tags=[f"tenant:{tenant_id}", f"model:{model}"],
        )
        gen = trace.generation(
            name=model,
            model=model,
            input=messages,
            output=response_text,
            usage={
                "input": tokens_in,
                "output": tokens_out,
                "unit": "TOKENS",
            },
            metadata={"latency_ms": latency_ms, "success": success},
        )
        gen.end()
        lf.flush()
    except Exception as exc:
        logger.debug("langfuse_trace_failed: %s", exc)
