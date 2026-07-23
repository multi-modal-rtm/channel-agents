"""
Single chokepoint for all Anthropic LLM calls (Rule 6).

Every call goes through TenantAwareAnthropicClient, which:
  - Resolves the API key (BYOK decrypted from DB, or platform managed key)
  - Checks per-tenant daily budget before calling
  - Records cost + latency to llm_calls after every call
  - Retries on 429 / 5xx with exponential backoff (tenacity)
  - Streams via a separate method that still cost-tracks at the end
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import anthropic
import structlog
from sqlalchemy import func, select
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.core.security import decrypt_api_key
from app.core.tenant_context import tenant_context
from app.db.models.llm_call import LLMCall
from app.db.models.tenant import Tenant
from app.db.session import get_admin_session, get_tenant_session

logger = structlog.get_logger(__name__)

# ── Model constants ────────────────────────────────────────────────────────────

MODEL_OPUS   = "claude-opus-4-7"
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_HAIKU  = "claude-haiku-4-5-20251001"

# Prices per token (USD). Prefix-matched so versioned IDs like
# "claude-haiku-4-5-20251001" resolve to "claude-haiku-4-5".
# Source: https://www.anthropic.com/pricing (verified Aug 2025)
MODEL_COSTS: dict[str, dict[str, float]] = {
    "claude-opus-4-7":   {"input": 15.00 / 1_000_000, "output": 75.00 / 1_000_000},
    "claude-sonnet-4-6": {"input":  3.00 / 1_000_000, "output": 15.00 / 1_000_000},
    "claude-haiku-4-5":  {"input":  0.80 / 1_000_000, "output":  4.00 / 1_000_000},
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _compute_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Return USD cost for a call. Matches on model-name prefix."""
    for prefix, rates in MODEL_COSTS.items():
        if model.startswith(prefix):
            return tokens_in * rates["input"] + tokens_out * rates["output"]
    logger.warning("unknown_model_cost", model=model)
    return 0.0


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, anthropic.RateLimitError):
        return True
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500


# ── Exceptions ────────────────────────────────────────────────────────────────

class BudgetExceededError(Exception):
    """Raised before an LLM call when the tenant's daily budget is exhausted."""

    def __init__(self, tenant_id: UUID, spent: float, limit: float) -> None:
        self.tenant_id = tenant_id
        self.spent = spent
        self.limit = limit
        super().__init__(
            f"Daily budget exceeded for tenant {tenant_id}: "
            f"${spent:.4f} spent >= ${limit:.4f} limit"
        )


# ── Client ────────────────────────────────────────────────────────────────────

class TenantAwareAnthropicClient:
    """
    Instantiate via ``await TenantAwareAnthropicClient.create(tenant_id)`` in
    production.  Pass ``_anthropic_client`` and ``_daily_budget_usd`` directly
    in tests to bypass DB loading.
    """

    def __init__(
        self,
        tenant_id: UUID,
        *,
        _anthropic_client: anthropic.AsyncAnthropic | None = None,
        _daily_budget_usd: float | None = None,
        _retry_wait: Any | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self._client = _anthropic_client
        self._daily_budget_usd = _daily_budget_usd
        # Configurable in tests so retries don't actually sleep.
        self._retry_wait = _retry_wait or wait_exponential(multiplier=1, min=1, max=30)

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    async def create(cls, tenant_id: UUID) -> "TenantAwareAnthropicClient":
        """
        Load tenant from DB, resolve API key (BYOK or platform managed).
        Decrypted BYOK key lives only in this object's memory (Rule 4).
        """
        async with get_admin_session() as session:
            result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
            tenant = result.scalar_one_or_none()

        if tenant is None:
            raise ValueError(f"Tenant {tenant_id} not found")

        if tenant.billing_mode == "byok":
            if tenant.anthropic_key_encrypted is None:
                raise ValueError(f"Tenant {tenant_id} is set to BYOK but has no key stored")
            api_key = decrypt_api_key(tenant.anthropic_key_encrypted)
        else:
            api_key = settings.anthropic_api_key_managed
            if not api_key:
                raise ValueError(
                    "ANTHROPIC_API_KEY_MANAGED is not configured for managed-billing tenants"
                )

        return cls(
            tenant_id=tenant_id,
            _anthropic_client=anthropic.AsyncAnthropic(api_key=api_key),
            _daily_budget_usd=tenant.daily_budget_usd,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = MODEL_SONNET,
        system: str | None = None,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
        agent_id: UUID | None = None,
        agent_type: str | None = None,
    ) -> anthropic.types.Message:
        """Non-streaming call. Budget checked before; cost logged in finally."""
        await self._check_budget()

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if system is not None:
            kwargs["system"] = system
        if tools is not None:
            kwargs["tools"] = tools

        t0 = time.monotonic()
        success = True
        response: anthropic.types.Message | None = None
        try:
            response = await self._call_with_retry(**kwargs)
            return response
        except Exception:
            success = False
            raise
        finally:
            latency_ms = int((time.monotonic() - t0) * 1000)
            tokens_in  = response.usage.input_tokens  if response else 0
            tokens_out = response.usage.output_tokens if response else 0
            cost = _compute_cost(model, tokens_in, tokens_out)
            resp_text = None
            try:
                if response:
                    for block in response.content:
                        if getattr(block, "type", None) == "text":
                            resp_text = block.text
                            break
            except Exception:
                pass
            await self._write_llm_call(
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
                latency_ms=latency_ms,
                success=success,
                agent_id=agent_id,
                agent_type=agent_type,
                response_text=resp_text,
                messages=messages,
            )
            logger.debug(
                "llm_call",
                tenant_id=str(self.tenant_id),
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=round(cost, 8),
                latency_ms=latency_ms,
                success=success,
            )

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = MODEL_SONNET,
        system: str | None = None,
        max_tokens: int = 1024,
        tools: list[dict[str, Any]] | None = None,
        agent_id: UUID | None = None,
    ) -> AsyncIterator[str]:
        """
        Streaming variant.  Budget is checked before the first chunk is yielded.
        Cost is logged after the stream finishes.

        Usage::

            async for chunk in await client.stream_chat(messages):
                print(chunk, end="", flush=True)
        """
        await self._check_budget()
        return self._stream_impl(
            messages=messages,
            model=model,
            system=system,
            max_tokens=max_tokens,
            tools=tools,
            agent_id=agent_id,
        )

    async def validate_key(self) -> bool:
        """Verify the resolved API key works. Called during BYOK key submission."""
        try:
            await self._client.messages.create(
                model=MODEL_HAIKU,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except anthropic.AuthenticationError:
            return False

    # ── Internals ────────────────────────────────────────────────────────────

    async def _stream_impl(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str,
        system: str | None,
        max_tokens: int,
        tools: list[dict[str, Any]] | None,
        agent_id: UUID | None,
    ) -> AsyncGenerator[str, None]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if system is not None:
            kwargs["system"] = system
        if tools is not None:
            kwargs["tools"] = tools

        t0 = time.monotonic()
        success = True
        tokens_in = tokens_out = 0
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    yield text
                final = await stream.get_final_message()
                tokens_in  = final.usage.input_tokens
                tokens_out = final.usage.output_tokens
        except Exception:
            success = False
            raise
        finally:
            latency_ms = int((time.monotonic() - t0) * 1000)
            cost = _compute_cost(model, tokens_in, tokens_out)
            await self._write_llm_call(
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
                latency_ms=latency_ms,
                success=success,
                agent_id=agent_id,
            )
            logger.debug(
                "llm_call_stream",
                tenant_id=str(self.tenant_id),
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=round(cost, 8),
                latency_ms=latency_ms,
                success=success,
            )

    async def _check_budget(self) -> None:
        if self._daily_budget_usd is None:
            return

        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        with tenant_context(self.tenant_id):
            async with get_tenant_session() as session:
                result = await session.execute(
                    select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(
                        LLMCall.tenant_id == self.tenant_id,
                        LLMCall.created_at >= today,
                    )
                )
                spent = float(result.scalar() or 0)

        if spent >= self._daily_budget_usd:
            logger.warning(
                "budget_exceeded",
                tenant_id=str(self.tenant_id),
                spent=spent,
                limit=self._daily_budget_usd,
            )
            raise BudgetExceededError(self.tenant_id, spent, self._daily_budget_usd)

        if spent >= self._daily_budget_usd * 0.8:
            logger.warning(
                "budget_soft_warning",
                tenant_id=str(self.tenant_id),
                spent=round(spent, 6),
                limit=self._daily_budget_usd,
                pct=round(spent / self._daily_budget_usd * 100, 1),
            )

    async def _call_with_retry(self, **kwargs: Any) -> anthropic.types.Message:
        @_retry_decorator(self._retry_wait)
        async def _call() -> anthropic.types.Message:
            return await self._client.messages.create(**kwargs)

        return await _call()

    async def _write_llm_call(
        self,
        *,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        latency_ms: int,
        success: bool,
        agent_id: UUID | None,
        agent_type: str | None = None,
        response_text: str | None = None,
        messages: list | None = None,
    ) -> None:
        # ── Prometheus (sync, never fails the caller) ─────────────────────────
        try:
            from app.core.metrics import llm_calls_total, llm_cost_usd_total
            llm_calls_total.labels(model=model, success=str(success)).inc()
            if cost_usd:
                llm_cost_usd_total.labels(model=model).inc(cost_usd)
        except Exception:
            pass

        # ── Langfuse (async fire-and-forget) ──────────────────────────────────
        try:
            from app.core.langfuse import trace_llm_call
            trace_llm_call(
                tenant_id=self.tenant_id,
                agent_type=agent_type,
                model=model,
                messages=messages or [],
                response_text=response_text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                success=success,
            )
        except Exception:
            pass

        # ── PostgreSQL (Rule 6: must record, but failures never block) ────────
        try:
            with tenant_context(self.tenant_id):
                async with get_tenant_session() as session:
                    session.add(
                        LLMCall(
                            tenant_id=self.tenant_id,
                            agent_id=agent_id,
                            model=model,
                            tokens_in=tokens_in,
                            tokens_out=tokens_out,
                            cost_usd=cost_usd,
                            latency_ms=latency_ms,
                            success=success,
                        )
                    )
                    await session.commit()
        except Exception as exc:
            logger.error(
                "llm_call_log_failed",
                error=str(exc),
                tenant_id=str(self.tenant_id),
            )


def _retry_decorator(wait: Any):
    """Build a tenacity retry decorator with the given wait strategy."""
    from tenacity import retry

    return retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait,
        stop=stop_after_attempt(3),
        reraise=True,
    )
