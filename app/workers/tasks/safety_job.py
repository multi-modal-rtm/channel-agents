"""Background safety job — runs every 5 minutes.

For every active tenant:
  1. Budget > 95% of daily_budget_usd → auto-pause + alert
  2. Error rate in last 15 min > 20% → auto-pause + alert
  3. Any agent with > 10 consecutive failures → disable that agent

Alerts write to audit_log and log at WARNING level.
In production a real notification adapter (email/Telegram) would be wired here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import safety_job_actions_total, tenant_paused_total
from app.core.tenant_context import tenant_context
from app.db.models.agent import Agent
from app.db.models.audit_log import AuditLog
from app.db.models.llm_call import LLMCall
from app.db.models.tenant import Tenant
from app.db.session import get_admin_session, get_tenant_session

logger = structlog.get_logger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
BUDGET_WARN_PCT = 0.95          # 95 % of daily budget triggers auto-pause
ERROR_RATE_WINDOW = timedelta(minutes=15)
ERROR_RATE_THRESHOLD = 0.20     # 20% failures in window
MIN_CALLS_FOR_RATE_CHECK = 5    # ignore windows with < 5 calls
CONSECUTIVE_FAILURE_LIMIT = 10


# ── Entry point ───────────────────────────────────────────────────────────────

async def run_safety_checks(ctx: dict[str, Any]) -> None:
    """arq cron entry point."""
    logger.info("safety_job_start")
    safety_job_actions_total.labels(action="run").inc()

    async with get_admin_session() as session:
        result = await session.execute(
            select(Tenant).where(Tenant.status == "active")
        )
        tenants = result.scalars().all()

    for tenant in tenants:
        try:
            await _check_tenant(tenant)
        except Exception as exc:
            logger.exception("safety_job_tenant_error tenant=%s: %s", tenant.id, exc)


# ── Per-tenant checks ─────────────────────────────────────────────────────────

async def _check_tenant(tenant: Tenant) -> None:
    with tenant_context(tenant.id):
        async with get_tenant_session() as session:
            paused = await _check_budget(tenant, session)
            if not paused:
                await _check_error_rate(tenant, session)
            await _check_consecutive_failures(tenant, session)
            await session.commit()


async def _check_budget(tenant: Tenant, session: AsyncSession) -> bool:
    """Return True (and pause tenant) if today's spend ≥ 95% of budget."""
    if tenant.daily_budget_usd is None:
        return False

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await session.execute(
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(
            LLMCall.tenant_id == tenant.id,
            LLMCall.created_at >= today,
        )
    )
    spent = float(result.scalar() or 0)
    budget = float(tenant.daily_budget_usd)

    if spent < budget * BUDGET_WARN_PCT:
        return False

    logger.warning(
        "safety_budget_exceeded",
        tenant_id=str(tenant.id),
        spent=spent,
        budget=budget,
        pct=round(spent / budget * 100, 1),
    )
    await _pause_tenant(tenant, session, reason="budget_95pct")
    await _send_alert(tenant, f"Budget alert: spent ${spent:.2f} of ${budget:.2f} (≥95%)")
    return True


async def _check_error_rate(tenant: Tenant, session: AsyncSession) -> bool:
    """Return True (and pause tenant) if error rate in last 15 min > 20%."""
    since = datetime.now(UTC) - ERROR_RATE_WINDOW
    result = await session.execute(
        select(
            func.count(LLMCall.id).label("total"),
            func.sum(
                func.cast(~LLMCall.success, LLMCall.tokens_in.type)
            ).label("errors"),
        ).where(
            LLMCall.tenant_id == tenant.id,
            LLMCall.created_at >= since,
        )
    )
    row = result.one()
    total = int(row.total or 0)
    errors = int(row.errors or 0)

    if total < MIN_CALLS_FOR_RATE_CHECK:
        return False

    rate = errors / total
    if rate <= ERROR_RATE_THRESHOLD:
        return False

    logger.warning(
        "safety_error_rate_exceeded",
        tenant_id=str(tenant.id),
        errors=errors,
        total=total,
        rate=round(rate * 100, 1),
    )
    await _pause_tenant(tenant, session, reason="error_rate_20pct")
    await _send_alert(
        tenant,
        f"Error rate alert: {errors}/{total} calls failed ({rate*100:.0f}%) in last 15 min",
    )
    return True


async def _check_consecutive_failures(tenant: Tenant, session: AsyncSession) -> None:
    """Disable any agent with > CONSECUTIVE_FAILURE_LIMIT consecutive failures."""
    agents_result = await session.execute(
        select(Agent).where(Agent.tenant_id == tenant.id, Agent.enabled == True)
    )
    agents = agents_result.scalars().all()

    for agent in agents:
        consecutive = await _count_consecutive_failures(agent.id, tenant.id, session)
        if consecutive >= CONSECUTIVE_FAILURE_LIMIT:
            logger.warning(
                "safety_agent_consecutive_failures",
                tenant_id=str(tenant.id),
                agent_id=str(agent.id),
                agent_type=agent.type,
                consecutive=consecutive,
            )
            agent.enabled = False
            session.add(AuditLog(
                tenant_id=tenant.id,
                action="agent.auto_disable",
                entity_type="agent",
                entity_id=agent.id,
                payload_json={
                    "reason": "consecutive_failures",
                    "count": consecutive,
                },
            ))
            safety_job_actions_total.labels(action="agent_disabled").inc()
            await _send_alert(
                tenant,
                f"Agent {agent.type} ({agent.id}) auto-disabled after "
                f"{consecutive} consecutive failures",
            )


async def _count_consecutive_failures(
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID,
    session: AsyncSession,
) -> int:
    """Count consecutive failures from the most recent call backwards."""
    result = await session.execute(
        select(LLMCall.success)
        .where(LLMCall.tenant_id == tenant_id, LLMCall.agent_id == agent_id)
        .order_by(LLMCall.created_at.desc())
        .limit(CONSECUTIVE_FAILURE_LIMIT + 5)
    )
    successes = [row[0] for row in result.all()]
    count = 0
    for s in successes:
        if not s:
            count += 1
        else:
            break
    return count


# ── Side-effects ──────────────────────────────────────────────────────────────

async def _pause_tenant(tenant: Tenant, session: AsyncSession, *, reason: str) -> None:
    """Update DB status to 'paused' and write audit log."""
    async with get_admin_session() as admin:
        result = await admin.execute(select(Tenant).where(Tenant.id == tenant.id))
        db_tenant = result.scalar_one()
        db_tenant.status = "paused"
        db_tenant.autonomous_actions_enabled = False
        await admin.commit()

    session.add(AuditLog(
        tenant_id=tenant.id,
        action="tenant.auto_pause",
        entity_type="tenant",
        entity_id=tenant.id,
        payload_json={"reason": reason},
    ))
    tenant_paused_total.labels(reason=reason).inc()
    safety_job_actions_total.labels(action="tenant_paused").inc()


async def _send_alert(tenant: Tenant, message: str) -> None:
    """Log the alert. Wire to email/Telegram in production."""
    logger.warning(
        "safety_alert",
        tenant_id=str(tenant.id),
        tenant_slug=tenant.slug,
        alert=message,
    )
    # TODO: send email via SendGrid / notify via Telegram bot
