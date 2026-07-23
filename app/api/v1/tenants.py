from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from uuid import UUID

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentTenant, require_role
from app.core.security import encrypt_api_key
from app.db.models.audit_log import AuditLog
from app.db.models.llm_call import LLMCall
from app.db.models.tenant import Tenant
from app.db.session import get_admin_session, get_rls_db, get_tenant_session
from app.schemas.tenant import AnthropicKeyRequest, TenantResponse, TenantUpdateRequest
from app.schemas.usage import TodayUsageResponse, UsageBreakdownItem, UsageResponse

router = APIRouter(prefix="/tenants", tags=["tenants"])

Owner = Annotated[type, Depends(require_role("owner"))]


# ── Read / update ─────────────────────────────────────────────────────────────

@router.get("/me")
async def get_my_tenant(tenant: CurrentTenant) -> TenantResponse:
    return TenantResponse.from_orm(tenant)


@router.patch("/me")
async def update_my_tenant(
    body: TenantUpdateRequest,
    user: Annotated[type, Depends(require_role("owner"))],
    tenant: CurrentTenant,
) -> TenantResponse:
    async with get_admin_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.id == tenant.id))
        db_tenant = result.scalar_one()
        if body.name is not None:
            db_tenant.name = body.name
        if body.plan is not None:
            db_tenant.plan = body.plan
        await session.commit()
        await session.refresh(db_tenant)

    async with get_tenant_session() as session:
        session.add(AuditLog(
            tenant_id=tenant.id,
            user_id=user.id,
            action="tenant.update",
            entity_type="tenant",
            entity_id=tenant.id,
            payload_json=body.model_dump(exclude_none=True),
        ))
        await session.commit()

    return TenantResponse.from_orm(db_tenant)


# ── Kill switch: pause / resume ───────────────────────────────────────────────

@router.post("/me/pause", status_code=status.HTTP_204_NO_CONTENT)
async def pause_tenant(
    user: Owner,
    tenant: CurrentTenant,
) -> None:
    """Pause all autonomous agent actions for this tenant (Rule 8).

    Sets status='paused' and autonomous_actions_enabled=False.
    In-flight tasks complete; queued events that check status are dropped.
    """
    async with get_admin_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.id == tenant.id))
        db_tenant = result.scalar_one()
        db_tenant.status = "paused"
        db_tenant.autonomous_actions_enabled = False
        await session.commit()

    async with get_tenant_session() as session:
        session.add(AuditLog(
            tenant_id=tenant.id,
            user_id=user.id,
            action="tenant.pause",
            entity_type="tenant",
            entity_id=tenant.id,
            payload_json={"triggered_by": "manual"},
        ))
        await session.commit()


@router.post("/me/resume", status_code=status.HTTP_204_NO_CONTENT)
async def resume_tenant(
    user: Owner,
    tenant: CurrentTenant,
) -> None:
    """Resume autonomous agent actions."""
    async with get_admin_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.id == tenant.id))
        db_tenant = result.scalar_one()
        db_tenant.status = "active"
        db_tenant.autonomous_actions_enabled = True
        await session.commit()

    async with get_tenant_session() as session:
        session.add(AuditLog(
            tenant_id=tenant.id,
            user_id=user.id,
            action="tenant.resume",
            entity_type="tenant",
            entity_id=tenant.id,
            payload_json={"triggered_by": "manual"},
        ))
        await session.commit()


# ── Cost / usage dashboard ────────────────────────────────────────────────────

@router.get("/me/usage/today", response_model=TodayUsageResponse)
async def usage_today(
    user: CurrentTenant,
    tenant: CurrentTenant,
    session: Annotated[AsyncSession, Depends(get_rls_db)],
) -> TodayUsageResponse:
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    result = await session.execute(
        select(
            func.coalesce(func.sum(LLMCall.cost_usd), 0).label("cost"),
            func.count(LLMCall.id).label("calls"),
        ).where(
            LLMCall.tenant_id == tenant.id,
            LLMCall.created_at >= today_start,
        )
    )
    row = result.one()
    cost = float(row.cost)
    calls = int(row.calls)

    budget = float(tenant.daily_budget_usd) if tenant.daily_budget_usd else None
    pct = round(cost / budget * 100, 1) if budget else None

    return TodayUsageResponse(
        date=today_start.date(),
        cost_usd=cost,
        call_count=calls,
        budget_usd=budget,
        budget_pct=pct,
    )


@router.get("/me/usage", response_model=UsageResponse)
async def usage_period(
    user: CurrentTenant,
    tenant: CurrentTenant,
    session: Annotated[AsyncSession, Depends(get_rls_db)],
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
) -> UsageResponse:
    if to_date < from_date:
        raise HTTPException(status_code=400, detail="'to' must be >= 'from'")

    from_dt = datetime(from_date.year, from_date.month, from_date.day, tzinfo=UTC)
    to_dt = datetime(to_date.year, to_date.month, to_date.day, 23, 59, 59, tzinfo=UTC)

    breakdown_result = await session.execute(
        select(
            LLMCall.agent_id,
            LLMCall.model,
            func.count(LLMCall.id).label("call_count"),
            func.coalesce(func.sum(LLMCall.tokens_in), 0).label("tokens_in"),
            func.coalesce(func.sum(LLMCall.tokens_out), 0).label("tokens_out"),
            func.coalesce(func.sum(LLMCall.cost_usd), 0).label("cost_usd"),
            func.sum(
                func.cast(~LLMCall.success, type_=LLMCall.tokens_in.type)
            ).label("error_count"),
        )
        .where(
            LLMCall.tenant_id == tenant.id,
            LLMCall.created_at >= from_dt,
            LLMCall.created_at <= to_dt,
        )
        .group_by(LLMCall.agent_id, LLMCall.model)
        .order_by(func.sum(LLMCall.cost_usd).desc())
    )
    rows = breakdown_result.all()

    breakdown = [
        UsageBreakdownItem(
            agent_id=r.agent_id,
            model=r.model,
            call_count=int(r.call_count),
            tokens_in=int(r.tokens_in),
            tokens_out=int(r.tokens_out),
            cost_usd=float(r.cost_usd),
            error_count=int(r.error_count or 0),
        )
        for r in rows
    ]
    total_cost = sum(b.cost_usd for b in breakdown)
    total_calls = sum(b.call_count for b in breakdown)
    total_errors = sum(b.error_count for b in breakdown)

    return UsageResponse(
        from_date=from_date,
        to_date=to_date,
        total_cost_usd=total_cost,
        total_calls=total_calls,
        total_errors=total_errors,
        breakdown=breakdown,
    )


# ── BYOK key management ───────────────────────────────────────────────────────

@router.put("/me/anthropic-key", status_code=status.HTTP_204_NO_CONTENT)
async def set_anthropic_key(
    body: AnthropicKeyRequest,
    user: Annotated[type, Depends(require_role("owner"))],
    tenant: CurrentTenant,
) -> None:
    await _validate_anthropic_key(body.api_key)
    ciphertext = encrypt_api_key(body.api_key)
    async with get_admin_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.id == tenant.id))
        db_tenant = result.scalar_one()
        db_tenant.anthropic_key_encrypted = ciphertext
        db_tenant.billing_mode = "byok"
        await session.commit()
    async with get_tenant_session() as session:
        session.add(AuditLog(
            tenant_id=tenant.id,
            user_id=user.id,
            action="tenant.anthropic_key.set",
            entity_type="tenant",
            entity_id=tenant.id,
            payload_json={"billing_mode": "byok"},
        ))
        await session.commit()


@router.delete("/me/anthropic-key", status_code=status.HTTP_204_NO_CONTENT)
async def delete_anthropic_key(
    user: Annotated[type, Depends(require_role("owner"))],
    tenant: CurrentTenant,
) -> None:
    async with get_admin_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.id == tenant.id))
        db_tenant = result.scalar_one()
        db_tenant.anthropic_key_encrypted = None
        db_tenant.billing_mode = "managed"
        await session.commit()
    async with get_tenant_session() as session:
        session.add(AuditLog(
            tenant_id=tenant.id,
            user_id=user.id,
            action="tenant.anthropic_key.delete",
            entity_type="tenant",
            entity_id=tenant.id,
            payload_json={"billing_mode": "managed"},
        ))
        await session.commit()


async def _validate_anthropic_key(api_key: str) -> None:
    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
    except anthropic.AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid Anthropic API key",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not validate Anthropic key: {exc}",
        ) from exc
