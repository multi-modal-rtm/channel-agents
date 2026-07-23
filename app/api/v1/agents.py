"""Agent management endpoints.

GET  /agents           — list tenant's agents
PATCH /agents/{id}     — update config, system_prompt, autonomy_level, enabled
POST /agents/{id}/disable — emergency single-agent kill switch
POST /agents/{id}/test — run test input, no persistence
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentTenant, CurrentUser, require_role
from app.agents.base import AgentInput
from app.db.models.agent import Agent
from app.db.models.audit_log import AuditLog
from app.db.session import get_rls_db, get_tenant_session
from app.schemas.agent import AgentResponse, AgentTestRequest, AgentTestResponse, AgentUpdateRequest

router = APIRouter(prefix="/agents", tags=["agents"])

Owner = Annotated[type, Depends(require_role("owner", "admin"))]


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("/", response_model=list[AgentResponse])
async def list_agents(
    user: CurrentUser,
    tenant: CurrentTenant,
    session: Annotated[AsyncSession, Depends(get_rls_db)],
) -> list[AgentResponse]:
    result = await session.execute(
        select(Agent).where(Agent.tenant_id == tenant.id).order_by(Agent.type)
    )
    return [AgentResponse.model_validate(a) for a in result.scalars().all()]


# ── Update ────────────────────────────────────────────────────────────────────

@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: UUID,
    body: AgentUpdateRequest,
    user: CurrentUser,
    tenant: CurrentTenant,
    session: Annotated[AsyncSession, Depends(get_rls_db)],
) -> AgentResponse:
    result = await session.execute(
        select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant.id)
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    changed: dict = {}
    if body.name is not None:
        agent.name = body.name
        changed["name"] = body.name
    if body.system_prompt is not None:
        agent.system_prompt = body.system_prompt
        changed["system_prompt"] = "[updated]"   # never log full prompt (may contain secrets)
    if body.config_json is not None:
        agent.config_json = body.config_json
        changed["config_json"] = body.config_json
    if body.autonomy_level is not None:
        agent.autonomy_level = body.autonomy_level
        changed["autonomy_level"] = body.autonomy_level
    if body.enabled is not None:
        agent.enabled = body.enabled
        changed["enabled"] = body.enabled

    session.add(AuditLog(
        tenant_id=tenant.id,
        user_id=user.id,
        action="agent.update",
        entity_type="agent",
        entity_id=agent.id,
        payload_json=changed,
    ))
    await session.commit()
    await session.refresh(agent)
    return AgentResponse.model_validate(agent)


# ── Emergency disable ─────────────────────────────────────────────────────────

@router.post("/{agent_id}/disable", status_code=status.HTTP_204_NO_CONTENT)
async def disable_agent(
    agent_id: UUID,
    user: CurrentUser,
    tenant: CurrentTenant,
    session: Annotated[AsyncSession, Depends(get_rls_db)],
) -> None:
    result = await session.execute(
        select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant.id)
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent.enabled = False
    session.add(AuditLog(
        tenant_id=tenant.id,
        user_id=user.id,
        action="agent.disable",
        entity_type="agent",
        entity_id=agent.id,
        payload_json={"reason": "manual_emergency_disable"},
    ))
    await session.commit()


# ── Test run ──────────────────────────────────────────────────────────────────

@router.post("/{agent_id}/test", response_model=AgentTestResponse)
async def test_agent(
    agent_id: UUID,
    body: AgentTestRequest,
    user: CurrentUser,
    tenant: CurrentTenant,
) -> AgentTestResponse:
    """Run a test input through the agent. Side-effects are rolled back."""
    async with get_tenant_session() as session:
        result = await session.execute(
            select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant.id)
        )
        agent_record = result.scalar_one_or_none()
        if agent_record is None:
            raise HTTPException(status_code=404, detail="Agent not found")

        if not agent_record.enabled:
            raise HTTPException(status_code=409, detail="Agent is disabled")

        agent_cls = _agent_class(agent_record.type)
        if agent_cls is None:
            raise HTTPException(status_code=400, detail=f"Unknown agent type: {agent_record.type}")

        try:
            from app.integrations.anthropic_client import TenantAwareAnthropicClient
            llm_client = await TenantAwareAnthropicClient.create(tenant.id)
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        agent_obj = agent_cls(
            tenant_id=tenant.id,
            agent_db_record=agent_record,
            anthropic_client=llm_client,
            db_session=session,
        )

        output = await agent_obj.handle(
            AgentInput(
                type=body.input_type,
                payload=body.payload,
                channel=body.channel,
            )
        )
        # Roll back any audit-log rows the agent added — test runs are ephemeral
        await session.rollback()

    return AgentTestResponse(
        response_text=output.response_text,
        confidence=output.confidence,
        escalate_to_human=output.escalate_to_human,
        cost_usd=output.cost_usd,
        actions_taken=output.actions_taken,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _agent_class(agent_type: str):
    from app.agents.conversation import ConversationAgent
    from app.agents.content import ContentAgent
    from app.agents.lead_qualifier import LeadQualifierAgent
    from app.agents.orchestrator import OrchestratorAgent

    _MAP = {
        "conversation":   ConversationAgent,
        "content":        ContentAgent,
        "lead_qualifier": LeadQualifierAgent,
        "orchestrator":   OrchestratorAgent,
    }
    return _MAP.get(agent_type)
