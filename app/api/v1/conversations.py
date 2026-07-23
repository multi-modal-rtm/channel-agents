"""Conversation management endpoints.

GET  /conversations?status=&channel=&page=&page_size=  — paginated list
GET  /conversations/{id}                               — full thread + messages
POST /conversations/{id}/messages                      — human sends message
POST /conversations/{id}/escalate                      — mark as needs human
POST /conversations/{id}/close                         — close conversation
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.api.deps import CurrentTenant, CurrentUser
from app.db.models.audit_log import AuditLog
from app.db.models.conversation import Conversation, Message
from app.db.session import get_rls_db
from app.schemas.conversation import (
    ConversationDetail,
    ConversationOut,
    ConversationPage,
    EscalateRequest,
    MessageOut,
    SendMessageRequest,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("/", response_model=ConversationPage)
async def list_conversations(
    user: CurrentUser,
    tenant: CurrentTenant,
    session: Annotated[AsyncSession, Depends(get_rls_db)],
    channel: str | None = Query(None),
    conv_status: str | None = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> ConversationPage:
    q = select(Conversation).where(Conversation.tenant_id == tenant.id)
    if channel:
        q = q.where(Conversation.channel == channel)
    if conv_status:
        q = q.where(Conversation.status == conv_status)

    total_result = await session.execute(
        select(func.count()).select_from(q.subquery())
    )
    total = total_result.scalar_one()

    items_result = await session.execute(
        q.order_by(Conversation.last_message_at.desc().nulls_last())
         .offset((page - 1) * page_size)
         .limit(page_size)
    )
    items = [ConversationOut.model_validate(c) for c in items_result.scalars().all()]
    return ConversationPage(items=items, total=total, page=page, page_size=page_size)


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: UUID,
    user: CurrentUser,
    tenant: CurrentTenant,
    session: Annotated[AsyncSession, Depends(get_rls_db)],
    last_n: int = Query(50, ge=1, le=200),
) -> ConversationDetail:
    conv_result = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.tenant_id == tenant.id,
        )
    )
    conv = conv_result.scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    msgs_result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id, Message.tenant_id == tenant.id)
        .order_by(Message.created_at.asc())
        .limit(last_n)
    )
    messages = [MessageOut.model_validate(m) for m in msgs_result.scalars().all()]
    return ConversationDetail(**ConversationOut.model_validate(conv).model_dump(), messages=messages)


# ── Human sends a message (agent takeover) ────────────────────────────────────

@router.post("/{conversation_id}/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
async def send_human_message(
    conversation_id: UUID,
    body: SendMessageRequest,
    user: CurrentUser,
    tenant: CurrentTenant,
    session: Annotated[AsyncSession, Depends(get_rls_db)],
) -> MessageOut:
    conv = await _get_conv_or_404(conversation_id, tenant.id, session)

    msg = Message(
        tenant_id=tenant.id,
        conversation_id=conversation_id,
        role="agent",          # human operator speaks as "agent" in the thread
        content=body.content,
        human_user_id=user.id,
    )
    session.add(msg)
    session.add(AuditLog(
        tenant_id=tenant.id,
        user_id=user.id,
        action="conversation.human_message",
        entity_type="conversation",
        entity_id=conversation_id,
        payload_json={"length": len(body.content)},
    ))
    await session.commit()
    await session.refresh(msg)
    return MessageOut.model_validate(msg)


# ── Escalate ──────────────────────────────────────────────────────────────────

@router.post("/{conversation_id}/escalate", status_code=status.HTTP_204_NO_CONTENT)
async def escalate_conversation(
    conversation_id: UUID,
    body: EscalateRequest,
    user: CurrentUser,
    tenant: CurrentTenant,
    session: Annotated[AsyncSession, Depends(get_rls_db)],
) -> None:
    conv = await _get_conv_or_404(conversation_id, tenant.id, session)
    conv.status = "escalated"
    session.add(AuditLog(
        tenant_id=tenant.id,
        user_id=user.id,
        action="conversation.escalate",
        entity_type="conversation",
        entity_id=conversation_id,
        payload_json={"reason": body.reason},
    ))
    await session.commit()


# ── Close ─────────────────────────────────────────────────────────────────────

@router.post("/{conversation_id}/close", status_code=status.HTTP_204_NO_CONTENT)
async def close_conversation(
    conversation_id: UUID,
    user: CurrentUser,
    tenant: CurrentTenant,
    session: Annotated[AsyncSession, Depends(get_rls_db)],
) -> None:
    conv = await _get_conv_or_404(conversation_id, tenant.id, session)
    conv.status = "closed"
    session.add(AuditLog(
        tenant_id=tenant.id,
        user_id=user.id,
        action="conversation.close",
        entity_type="conversation",
        entity_id=conversation_id,
        payload_json={},
    ))
    await session.commit()


# ── Helper ────────────────────────────────────────────────────────────────────

async def _get_conv_or_404(conv_id: UUID, tenant_id: UUID, session: AsyncSession) -> Conversation:
    result = await session.execute(
        select(Conversation).where(
            Conversation.id == conv_id,
            Conversation.tenant_id == tenant_id,
        )
    )
    conv = result.scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv
