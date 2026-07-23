"""Process incoming Telegram updates: store message, enqueue background task."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models.conversation import Conversation, Message
from app.db.models.tenant import Tenant
from app.integrations.telegram.models import TelegramUpdate

logger = logging.getLogger(__name__)

_fernet = Fernet(settings.encryption_key.encode())


async def get_tenant_by_slug(slug: str, session: AsyncSession) -> Tenant | None:
    result = await session.execute(select(Tenant).where(Tenant.slug == slug))
    return result.scalar_one_or_none()


async def get_or_create_conversation(
    *,
    tenant_id: uuid.UUID,
    channel: str,
    customer_handle: str,
    customer_name: str | None,
    session: AsyncSession,
) -> Conversation:
    result = await session.execute(
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
        )
        session.add(conv)
        await session.flush()
    else:
        conv.last_message_at = datetime.now(UTC)
        if customer_name and not conv.customer_name:
            conv.customer_name = customer_name
    return conv


async def store_message(
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    session: AsyncSession,
) -> Message:
    msg = Message(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        role=role,
        content=content,
    )
    session.add(msg)
    await session.flush()
    return msg


async def process_update(
    *,
    update: TelegramUpdate,
    tenant: Tenant,
    session: AsyncSession,
    arq_redis,
) -> None:
    """
    Main entry point called by the webhook route.
    Stores the inbound message and enqueues the background task.

    Special case: /start <token> redeems a cross-channel handoff and loads
    Instagram conversation history for seamless continuation.
    """
    msg = update.effective_message
    if msg is None or not msg.effective_text:
        return

    sender = msg.from_
    telegram_chat_id = msg.chat.id
    customer_handle = str(sender.id) if sender else str(telegram_chat_id)
    customer_name = None
    if sender:
        parts = [sender.first_name, sender.last_name]
        customer_name = " ".join(p for p in parts if p) or None

    text = msg.effective_text

    # ── /start <handoff_token> — cross-channel identity merge ────────────────
    customer_id: str | None = None
    handoff_token = _extract_start_token(text)
    if handoff_token is not None:
        customer_id = await _redeem_handoff(
            token=handoff_token,
            tenant=tenant,
            telegram_chat_id=telegram_chat_id,
            customer_name=customer_name,
            session=session,
        )
        if customer_id is not None:
            # Replace "/start <token>" with a natural greeting for the agent
            text = "/start"

    conv = await get_or_create_conversation(
        tenant_id=tenant.id,
        channel="telegram",
        customer_handle=customer_handle,
        customer_name=customer_name,
        session=session,
    )

    # Link conversation to the unified customer record
    if customer_id and conv.customer_id is None:
        conv.customer_id = uuid.UUID(customer_id)
    elif customer_id is None:
        # Regular Telegram message — find or create a standalone TG customer
        try:
            from app.services.customer_service import CustomerService
            cs = CustomerService(session)
            customer = await cs.get_or_create_by_telegram(
                tenant_id=tenant.id,
                telegram_chat_id=telegram_chat_id,
                name=customer_name,
            )
            if conv.customer_id is None:
                conv.customer_id = customer.id
            customer_id = str(customer.id)
        except Exception as exc:
            logger.warning("tg_customer_resolution_failed", error=str(exc))

    await store_message(
        tenant_id=tenant.id,
        conversation_id=conv.id,
        role="user",
        content=text,
        session=session,
    )

    await session.commit()

    if arq_redis is not None:
        task_kwargs: dict = dict(
            tenant_id=str(tenant.id),
            conversation_id=str(conv.id),
            message_text=text,
            channel="telegram",
            chat_id=telegram_chat_id,
        )
        if customer_id:
            task_kwargs["customer_id"] = customer_id
        await arq_redis.enqueue_job("handle_incoming_message", **task_kwargs)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_start_token(text: str) -> uuid.UUID | None:
    """Return the UUID token from '/start <token>', or None if not present/invalid."""
    if not text.startswith("/start "):
        return None
    token_str = text[len("/start "):].strip()
    if not token_str:
        return None
    try:
        return uuid.UUID(token_str)
    except ValueError:
        return None


async def _redeem_handoff(
    *,
    token: uuid.UUID,
    tenant: Tenant,
    telegram_chat_id: int,
    customer_name: str | None,
    session: AsyncSession,
) -> str | None:
    """Redeem a handoff token and return the merged customer_id (str), or None on failure."""
    try:
        from app.services.customer_service import CustomerService
        cs = CustomerService(session)
        customer = await cs.redeem_handoff(
            token=token,
            telegram_chat_id=telegram_chat_id,
        )
        if customer is None:
            return None
        if customer_name and not customer.customer_name:
            customer.customer_name = customer_name
        logger.info(
            "handoff_redeemed",
            tenant_id=str(tenant.id),
            customer_id=str(customer.id),
            telegram_chat_id=telegram_chat_id,
        )
        return str(customer.id)
    except Exception as exc:
        logger.error("handoff_redemption_failed", token=str(token), error=str(exc))
        return None
