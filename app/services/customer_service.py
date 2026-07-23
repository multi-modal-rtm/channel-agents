"""CustomerService — unified customer identity across channels.

Responsibilities:
  - get_or_create_by_instagram / get_or_create_by_telegram:
      Find or create a Customer from a channel-specific identifier.
  - create_handoff:
      Mint a short-lived bridge token so an IG customer can continue on TG.
  - redeem_handoff:
      Validate token, link Telegram identity to the existing Customer, mark used.
  - get_cross_channel_history:
      Load the last N messages across all conversations for one customer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.conversation import Conversation, Message
from app.db.models.customer import Customer, CustomerHandoff

logger = structlog.get_logger(__name__)

_HANDOFF_TTL_MINUTES = 10


class CustomerService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Identity resolution ───────────────────────────────────────────────────

    async def get_or_create_by_instagram(
        self,
        *,
        tenant_id: uuid.UUID,
        instagram_psid: str,
        name: str | None = None,
    ) -> Customer:
        result = await self._session.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                Customer.instagram_psid == instagram_psid,
            )
        )
        customer = result.scalar_one_or_none()
        if customer is None:
            customer = Customer(
                tenant_id=tenant_id,
                instagram_psid=instagram_psid,
                customer_name=name,
            )
            self._session.add(customer)
            await self._session.flush()
        elif name and not customer.customer_name:
            customer.customer_name = name
        return customer

    async def get_or_create_by_telegram(
        self,
        *,
        tenant_id: uuid.UUID,
        telegram_chat_id: int,
        name: str | None = None,
    ) -> Customer:
        result = await self._session.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                Customer.telegram_chat_id == telegram_chat_id,
            )
        )
        customer = result.scalar_one_or_none()
        if customer is None:
            customer = Customer(
                tenant_id=tenant_id,
                telegram_chat_id=telegram_chat_id,
                customer_name=name,
            )
            self._session.add(customer)
            await self._session.flush()
        elif name and not customer.customer_name:
            customer.customer_name = name
        return customer

    # ── Handoff lifecycle ─────────────────────────────────────────────────────

    async def create_handoff(
        self,
        *,
        tenant_id: uuid.UUID,
        customer_id: uuid.UUID,
        channel_from: str = "instagram",
        channel_to: str = "telegram",
    ) -> CustomerHandoff:
        handoff = CustomerHandoff(
            tenant_id=tenant_id,
            customer_id=customer_id,
            channel_from=channel_from,
            channel_to=channel_to,
            expires_at=datetime.now(UTC) + timedelta(minutes=_HANDOFF_TTL_MINUTES),
        )
        self._session.add(handoff)
        await self._session.flush()
        return handoff

    async def redeem_handoff(
        self,
        *,
        token: uuid.UUID,
        telegram_chat_id: int,
        now: datetime | None = None,
    ) -> Customer | None:
        """Validate the token and link the Telegram identity to the existing customer.

        Returns the merged Customer on success, None if the token is invalid/expired/used.
        """
        now = now or datetime.now(UTC)

        result = await self._session.execute(
            select(CustomerHandoff).where(CustomerHandoff.id == token)
        )
        handoff = result.scalar_one_or_none()

        if handoff is None:
            logger.warning("handoff_not_found", token=str(token))
            return None
        if handoff.redeemed_at is not None:
            logger.warning("handoff_already_redeemed", token=str(token))
            return None
        if handoff.expires_at <= now:
            logger.warning("handoff_expired", token=str(token), expired_at=handoff.expires_at)
            return None

        # Mark used
        handoff.redeemed_at = now

        # Load the customer and attach the Telegram identity
        cust_result = await self._session.execute(
            select(Customer).where(Customer.id == handoff.customer_id)
        )
        customer = cust_result.scalar_one_or_none()
        if customer is None:
            logger.error("handoff_customer_missing", customer_id=str(handoff.customer_id))
            return None

        if customer.telegram_chat_id is None:
            customer.telegram_chat_id = telegram_chat_id
        elif customer.telegram_chat_id != telegram_chat_id:
            logger.warning(
                "handoff_chat_id_mismatch",
                existing=customer.telegram_chat_id,
                new=telegram_chat_id,
            )

        await self._session.flush()
        return customer

    # ── Cross-channel history ─────────────────────────────────────────────────

    async def get_cross_channel_history(
        self,
        *,
        tenant_id: uuid.UUID,
        customer_id: uuid.UUID,
        last_n: int = 20,
    ) -> list[dict[str, Any]]:
        """Return the last ``last_n`` messages across ALL conversations for this customer.

        Results are ordered oldest-first, suitable for passing to the LLM messages list.
        """
        result = await self._session.execute(
            select(Message, Conversation.channel)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.customer_id == customer_id,
            )
            .order_by(Message.created_at.desc())
            .limit(last_n)
        )
        rows = result.all()
        # rows = [(Message, channel), ...] ordered newest-first; reverse for LLM
        return [
            {"role": msg.role, "content": msg.content}
            for msg, _channel in reversed(rows)
        ]
