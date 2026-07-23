"""ConversationService — DB-backed conversation and message management."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.conversation import Conversation, Message


class ConversationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(
        self,
        *,
        tenant_id: UUID,
        channel: str,
        customer_handle: str,
        customer_name: str | None = None,
    ) -> Conversation:
        result = await self._session.execute(
            select(Conversation).where(
                Conversation.tenant_id == tenant_id,
                Conversation.channel == channel,
                Conversation.customer_handle == customer_handle,
            )
        )
        conv = result.scalar_one_or_none()
        if conv is None:
            conv = Conversation(
                tenant_id=tenant_id,
                channel=channel,
                customer_handle=customer_handle,
                customer_name=customer_name,
                last_message_at=datetime.now(UTC),
            )
            self._session.add(conv)
            await self._session.flush()
        else:
            conv.last_message_at = datetime.now(UTC)
            if customer_name and not conv.customer_name:
                conv.customer_name = customer_name
        return conv

    async def add_message(
        self,
        *,
        tenant_id: UUID,
        conversation_id: UUID,
        role: str,
        content: str,
        agent_id: UUID | None = None,
        cost_usd: float | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> Message:
        msg = Message(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            agent_id=agent_id,
            cost_usd=cost_usd,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        self._session.add(msg)
        await self._session.flush()
        return msg

    async def get_history(
        self,
        *,
        tenant_id: UUID,
        conversation_id: UUID,
        last_n: int = 10,
    ) -> list[dict]:
        result = await self._session.execute(
            select(Message)
            .where(
                Message.tenant_id == tenant_id,
                Message.conversation_id == conversation_id,
            )
            .order_by(Message.created_at.desc())
            .limit(last_n)
        )
        rows = result.scalars().all()
        # Return oldest-first for the LLM messages list
        return [{"role": r.role, "content": r.content} for r in reversed(rows)]
