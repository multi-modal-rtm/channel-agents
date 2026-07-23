"""Process inbound Meta/Instagram webhook payloads.

Signature verification and routing live here so the FastAPI route stays thin.
The route must return 200 within 1 second; real work is enqueued to arq.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.conversation import Conversation, Message
from app.db.models.tenant import Tenant

logger = structlog.get_logger(__name__)


# ── Signature verification ────────────────────────────────────────────────────

def verify_signature(body: bytes, app_secret: str, header: str | None) -> bool:
    """Verify X-Hub-Signature-256 header against HMAC-SHA256 of the raw body."""
    if not header or not header.startswith("sha256="):
        return False
    received = header.removeprefix("sha256=")
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


# ── Tenant lookup ─────────────────────────────────────────────────────────────

async def get_tenant_by_page_id(page_id: str, session: AsyncSession) -> Tenant | None:
    result = await session.execute(
        select(Tenant).where(Tenant.instagram_page_id == page_id)
    )
    return result.scalar_one_or_none()


# ── Main entry point ──────────────────────────────────────────────────────────

async def process_instagram_webhook(
    payload: dict[str, Any],
    *,
    session: AsyncSession,
    arq_redis: Any,
) -> None:
    """Route each entry in the payload to the appropriate handler."""
    if payload.get("object") not in ("instagram", "page"):
        return

    for entry in payload.get("entry", []):
        page_id = str(entry.get("id", ""))
        if not page_id:
            continue

        tenant = await get_tenant_by_page_id(page_id, session)
        if tenant is None:
            logger.warning("instagram_webhook_unknown_page", page_id=page_id)
            continue

        if tenant.status == "paused":
            logger.info("instagram_webhook_tenant_paused", tenant_id=str(tenant.id))
            continue

        # Direct messages
        for event in entry.get("messaging", []):
            await _handle_message_event(event, tenant=tenant, session=session, arq_redis=arq_redis)

        # Postbacks (button taps)
        for event in entry.get("messaging_postbacks", []):
            await _handle_postback_event(event, tenant=tenant, session=session, arq_redis=arq_redis)

        # Comments / other field changes
        for change in entry.get("changes", []):
            await _handle_change_event(change, tenant=tenant, session=session, arq_redis=arq_redis)


# ── Event handlers ────────────────────────────────────────────────────────────

async def _handle_message_event(
    event: dict[str, Any],
    *,
    tenant: Tenant,
    session: AsyncSession,
    arq_redis: Any,
) -> None:
    message = event.get("message", {})
    text = message.get("text", "")
    if not text:
        return

    sender_id = str(event.get("sender", {}).get("id", ""))
    if not sender_id:
        return

    # Detect story reply: message.reply_to.story.id
    story = message.get("reply_to", {}).get("story", {})
    story_id = str(story.get("id", "")) if story else ""

    conv = await _get_or_create_conversation(
        tenant_id=tenant.id,
        channel="instagram",
        customer_handle=sender_id,
        session=session,
    )

    # Resolve unified customer identity
    customer_id: str | None = None
    try:
        from app.services.customer_service import CustomerService
        cs = CustomerService(session)
        customer = await cs.get_or_create_by_instagram(
            tenant_id=tenant.id,
            instagram_psid=sender_id,
        )
        if conv.customer_id is None:
            conv.customer_id = customer.id
        customer_id = str(customer.id)
    except Exception as exc:
        logger.warning("customer_resolution_failed", sender_id=sender_id, error=str(exc))

    session.add(Message(
        tenant_id=tenant.id,
        conversation_id=conv.id,
        role="user",
        content=text,
    ))
    await session.flush()
    await session.commit()

    task_kwargs: dict[str, Any] = dict(
        tenant_id=str(tenant.id),
        conversation_id=str(conv.id),
        message_text=text,
        channel="instagram",
        chat_id=sender_id,
    )
    if story_id:
        task_kwargs["story_id"] = story_id
    if customer_id:
        task_kwargs["customer_id"] = customer_id

    if arq_redis is not None:
        await arq_redis.enqueue_job("handle_incoming_message", **task_kwargs)

    logger.info(
        "instagram_message_received",
        tenant_id=str(tenant.id),
        sender_id=sender_id,
        conversation_id=str(conv.id),
        story_reply=bool(story_id),
    )


async def _handle_postback_event(
    event: dict[str, Any],
    *,
    tenant: Tenant,
    session: AsyncSession,
    arq_redis: Any,
) -> None:
    postback = event.get("postback", {})
    raw_payload = postback.get("payload", "")
    title = postback.get("title", "")

    # Translate structured payloads into natural-language agent input
    message_text = _interpret_postback(raw_payload, title)
    if not message_text:
        return

    sender_id = str(event.get("sender", {}).get("id", ""))
    if not sender_id:
        return

    conv = await _get_or_create_conversation(
        tenant_id=tenant.id,
        channel="instagram",
        customer_handle=sender_id,
        session=session,
    )

    session.add(Message(
        tenant_id=tenant.id,
        conversation_id=conv.id,
        role="user",
        content=message_text,
    ))
    await session.flush()
    await session.commit()

    if arq_redis is not None:
        await arq_redis.enqueue_job(
            "handle_incoming_message",
            tenant_id=str(tenant.id),
            conversation_id=str(conv.id),
            message_text=message_text,
            channel="instagram",
            chat_id=sender_id,
        )

    logger.info(
        "instagram_postback_received",
        tenant_id=str(tenant.id),
        sender_id=sender_id,
        raw_payload=raw_payload,
    )


async def _handle_change_event(
    change: dict[str, Any],
    *,
    tenant: Tenant,
    session: AsyncSession,
    arq_redis: Any,
) -> None:
    field = change.get("field", "")
    if field != "comments":
        return

    value = change.get("value", {})
    text = value.get("text", "")
    comment_id = str(value.get("id", ""))
    commenter_id = str(value.get("from", {}).get("id", ""))
    post_id = str(value.get("media", {}).get("id", ""))
    if not text or not commenter_id:
        return

    conv = await _get_or_create_conversation(
        tenant_id=tenant.id,
        channel="instagram_comment",
        customer_handle=commenter_id,
        session=session,
    )

    session.add(Message(
        tenant_id=tenant.id,
        conversation_id=conv.id,
        role="user",
        content=text,
    ))
    await session.flush()
    await session.commit()

    if arq_redis is not None:
        await arq_redis.enqueue_job(
            "handle_incoming_message",
            tenant_id=str(tenant.id),
            conversation_id=str(conv.id),
            message_text=text,
            channel="instagram_comment",
            chat_id=commenter_id,
            comment_id=comment_id or None,
            post_id=post_id or None,
        )


def _interpret_postback(payload: str, title: str) -> str:
    """Convert a button postback payload into a natural-language agent message.

    Known payload prefixes:
      ORDER_{SKU}  — customer tapped the Order button on a product card
      VIEW_{SKU}   — customer tapped a View/Details button
    Falls back to the button title, then the raw payload.
    """
    if payload.startswith("ORDER_"):
        sku = payload[len("ORDER_"):]
        return f"I want to order product {sku}"
    if payload.startswith("VIEW_"):
        sku = payload[len("VIEW_"):]
        return f"Tell me more about product {sku}"
    return title or payload


async def _get_or_create_conversation(
    *,
    tenant_id: uuid.UUID,
    channel: str,
    customer_handle: str,
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
        )
        session.add(conv)
        await session.flush()
    return conv
