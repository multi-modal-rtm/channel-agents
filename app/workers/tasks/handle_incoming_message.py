"""arq background task: process an inbound message through the agent pipeline.

Called by the webhook handler after storing the customer message.
On success: stores the agent reply and sends it via the channel client.
On error: writes an audit_log entry with action='agent_error'.

instagram_comment channel bypasses the orchestrator and routes directly to
CommentResponderAgent, which returns structured JSON (action/public_text/dm_text).
Public replies go to reply_to_comment(); DMs go to send_message().
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from app.agents.base import AgentInput
from app.agents.channel_adapters import get_adapter
from app.agents.comment_responder import CommentResponderAgent
from app.agents.content import ContentAgent
from app.agents.conversation import ConversationAgent
from app.agents.lead_qualifier import LeadQualifierAgent
from app.agents.orchestrator import OrchestratorAgent
from app.config import settings
from app.core.tenant_context import tenant_context
from app.db.models.agent import Agent
from app.db.models.audit_log import AuditLog
from app.db.models.tenant import Tenant
from app.db.session import get_admin_session, get_tenant_session
from app.integrations.anthropic_client import TenantAwareAnthropicClient
from app.services.conversation_service import ConversationService

logger = logging.getLogger(__name__)

_fernet = Fernet(settings.encryption_key.encode())


async def handle_incoming_message(
    ctx: dict[str, Any],
    *,
    tenant_id: str,
    conversation_id: str,
    message_text: str,
    channel: str,
    chat_id: int | str | None = None,
    comment_id: str | None = None,
    post_id: str | None = None,
    story_id: str | None = None,
    customer_id: str | None = None,
) -> None:
    tid = uuid.UUID(tenant_id)
    cid = uuid.UUID(conversation_id)

    try:
        await _process(
            tenant_id=tid,
            conversation_id=cid,
            message_text=message_text,
            channel=channel,
            chat_id=chat_id,
            comment_id=comment_id,
            post_id=post_id,
            story_id=story_id,
            customer_id=customer_id,
        )
    except Exception as exc:
        logger.exception("agent_error tenant=%s conversation=%s", tenant_id, conversation_id)
        await _write_error_audit(tid, str(exc))


async def _process(
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_text: str,
    channel: str,
    chat_id: int | str | None,
    comment_id: str | None,
    post_id: str | None,
    story_id: str | None,
    customer_id: str | None,
) -> None:
    async with get_admin_session() as admin_session:
        tenant_result = await admin_session.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        )
        tenant = tenant_result.scalar_one_or_none()
        if tenant is None:
            logger.error("tenant not found: %s", tenant_id)
            return

    if tenant.status == "paused" or not tenant.autonomous_actions_enabled:
        logger.info(
            "tenant_paused_dropping_event",
            extra={"tenant_id": str(tenant_id), "status": tenant.status},
        )
        return

    with tenant_context(tenant_id):
        async with get_tenant_session() as session:
            agents_result = await session.execute(
                select(Agent).where(
                    Agent.tenant_id == tenant_id,
                    Agent.enabled == True,
                )
            )
            agent_records = {a.type: a for a in agents_result.scalars().all()}
            anthropic_client = await TenantAwareAnthropicClient.create(tenant_id)

            # ── instagram_comment: direct route to CommentResponderAgent ─────
            if channel == "instagram_comment":
                output = await _handle_comment(
                    tenant_id=tenant_id,
                    message_text=message_text,
                    comment_id=comment_id,
                    post_id=post_id,
                    agent_records=agent_records,
                    anthropic_client=anthropic_client,
                    session=session,
                )
                await session.commit()

            # ── All other channels: orchestrator pipeline ─────────────────────
            else:
                # Load history: prefer cross-channel when customer_id is known
                if customer_id:
                    from app.services.customer_service import CustomerService
                    cs = CustomerService(session)
                    history = await cs.get_cross_channel_history(
                        tenant_id=tenant_id,
                        customer_id=uuid.UUID(customer_id),
                        last_n=20,
                    )
                else:
                    conv_service = ConversationService(session)
                    history = await conv_service.get_history(
                        tenant_id=tenant_id,
                        conversation_id=conversation_id,
                        last_n=10,
                    )
                if history and history[-1]["role"] == "user":
                    history = history[:-1]

                # Story reply: prefix message so the agent has context
                if story_id:
                    payload_text = (
                        f"[Customer replied to your Instagram story]\n{message_text}"
                    )
                else:
                    payload_text = message_text

                def _make_specialist(agent_type: str, cls):
                    rec = agent_records.get(agent_type)
                    if rec is None:
                        return None
                    return cls(
                        tenant_id=tenant_id,
                        agent_db_record=rec,
                        anthropic_client=anthropic_client,
                        db_session=session,
                    )

                conversation_agent = _make_specialist("conversation", ConversationAgent)
                content_agent      = _make_specialist("content", ContentAgent)
                lead_agent         = _make_specialist("lead_qualifier", LeadQualifierAgent)

                specialists = {}
                if conversation_agent:
                    specialists["conversation"] = conversation_agent
                if content_agent:
                    specialists["content"] = content_agent
                if lead_agent:
                    specialists["lead_qualifier"] = lead_agent

                orch_rec = agent_records.get("orchestrator")
                if orch_rec is None:
                    logger.warning("no orchestrator agent for tenant %s", tenant_id)
                    return

                orchestrator = OrchestratorAgent(
                    tenant_id=tenant_id,
                    agent_db_record=orch_rec,
                    anthropic_client=anthropic_client,
                    db_session=session,
                    specialists=specialists,
                )

                agent_input = AgentInput(
                    type="message",
                    payload=payload_text,
                    context_history=history,
                    channel=channel,
                    customer_id=customer_id,
                )
                output = await orchestrator.handle(agent_input)
                await session.commit()

        # ── Format and store reply ────────────────────────────────────────────
        if channel == "instagram_comment":
            await _dispatch_comment_reply(
                output=output,
                tenant=tenant,
                chat_id=chat_id,
                comment_id=comment_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
        else:
            adapter = get_adapter(channel)
            formatted_text: str | None = None
            structured_payload: dict | None = None

            if output.carousel_products:
                structured_payload = adapter.format_product_carousel(output.carousel_products)
                # Store the fallback text (or a generic description) for conversation history
                formatted_text = output.response_text or "Product carousel"
            elif output.quick_reply_options and output.response_text:
                base_text = adapter.format_message(output.response_text)
                structured_payload = adapter.format_quick_replies(base_text, output.quick_reply_options)
                formatted_text = base_text
            elif output.response_text:
                formatted_text = adapter.format_message(output.response_text)

            with tenant_context(tenant_id):
                async with get_tenant_session() as session:
                    conv_service2 = ConversationService(session)
                    if formatted_text:
                        await conv_service2.add_message(
                            tenant_id=tenant_id,
                            conversation_id=conversation_id,
                            role="assistant",
                            content=formatted_text,
                            cost_usd=output.cost_usd or None,
                        )
                    await session.commit()

            if not output.escalate_to_human:
                if structured_payload is not None and chat_id is not None:
                    await _send_structured_reply(
                        tenant=tenant,
                        channel=channel,
                        chat_id=chat_id,
                        payload=structured_payload,
                    )
                elif formatted_text:
                    await _send_reply(
                        tenant=tenant,
                        channel=channel,
                        chat_id=chat_id,
                        text=formatted_text,
                    )
            elif output.escalate_to_human:
                logger.info(
                    "escalated conversation=%s tenant=%s", conversation_id, tenant_id
                )


async def _handle_comment(
    *,
    tenant_id: uuid.UUID,
    message_text: str,
    comment_id: str | None,
    post_id: str | None,
    agent_records: dict,
    anthropic_client,
    session,
) -> Any:
    """Instantiate CommentResponderAgent and run it for an instagram_comment event."""
    from app.agents.base import AgentOutput

    rec = agent_records.get("comment_responder")
    if rec is None:
        logger.warning("no comment_responder agent for tenant %s", tenant_id)
        return AgentOutput(response_text=None)

    agent = CommentResponderAgent(
        tenant_id=tenant_id,
        agent_db_record=rec,
        anthropic_client=anthropic_client,
        db_session=session,
    )

    agent_input = AgentInput(
        type="event",
        payload={
            "comment_text": message_text,
            "comment_id": comment_id or "",
            "post_id": post_id or "",
            "post_caption": "",  # future: fetch from Instagram API or DB
        },
        channel="instagram_comment",
    )
    return await agent.handle(agent_input)


async def _dispatch_comment_reply(
    *,
    output: Any,
    tenant: Tenant,
    chat_id: int | str | None,
    comment_id: str | None,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> None:
    """Parse structured output and send public reply and/or DM."""
    if not output.response_text:
        return

    try:
        decision = json.loads(output.response_text)
    except (json.JSONDecodeError, TypeError):
        logger.warning("comment_responder invalid JSON output: %s", output.response_text[:200])
        return

    action = decision.get("action", "ignore")
    public_text = decision.get("public_text")
    dm_text = decision.get("dm_text")

    if action == "ignore":
        if output.escalate_to_human:
            logger.info(
                "comment_escalated tenant=%s comment_id=%s", tenant_id, comment_id
            )
        return

    if not tenant.instagram_page_access_token_encrypted:
        logger.warning("no instagram token for tenant %s", tenant.id)
        return

    try:
        from app.integrations.meta.client import InstagramClient
        client = await InstagramClient.create(tenant.id)
    except Exception as exc:
        logger.error("instagram_client_create_failed tenant=%s: %s", tenant.id, exc)
        return

    # Public reply to the comment
    if public_text and comment_id:
        try:
            await client.reply_to_comment(comment_id, public_text)
            logger.info("comment_replied comment_id=%s", comment_id)
        except Exception as exc:
            logger.error("comment_reply_failed comment_id=%s: %s", comment_id, exc)

    # Private DM (only for public_reply_and_dm)
    if action == "public_reply_and_dm" and dm_text and chat_id:
        try:
            await client.send_message(str(chat_id), dm_text)
            logger.info("comment_dm_sent commenter=%s", chat_id)
        except Exception as exc:
            logger.error("comment_dm_failed commenter=%s: %s", chat_id, exc)

    if output.escalate_to_human:
        logger.info(
            "comment_flagged_for_human tenant=%s comment_id=%s", tenant_id, comment_id
        )


async def _send_structured_reply(
    *,
    tenant: Tenant,
    channel: str,
    chat_id: int | str,
    payload: dict,
) -> None:
    """Send a structured Instagram message (quick replies / carousel template)."""
    if channel not in ("instagram",):
        return
    if not tenant.instagram_page_access_token_encrypted:
        logger.warning("no instagram token for tenant %s", tenant.id)
        return
    try:
        from app.integrations.meta.client import InstagramClient
        client = await InstagramClient.create(tenant.id)
        await client.send_structured_message(str(chat_id), payload)
    except Exception as exc:
        logger.error(
            "failed to send structured instagram message tenant=%s: %s", tenant.id, exc
        )


async def _send_reply(
    *,
    tenant: Tenant,
    channel: str,
    chat_id: int | str | None,
    text: str,
) -> None:
    if channel == "telegram" and chat_id is not None:
        if not tenant.telegram_bot_token_encrypted:
            logger.warning("no telegram bot token for tenant %s", tenant.id)
            return
        try:
            bot_token = _fernet.decrypt(tenant.telegram_bot_token_encrypted).decode()
        except (InvalidToken, Exception) as exc:
            logger.error("failed to decrypt bot token for tenant %s: %s", tenant.id, exc)
            return
        from app.integrations.telegram.client import TelegramClient
        client = TelegramClient(bot_token)
        await client.send_message(chat_id, text)

    elif channel == "instagram" and chat_id is not None:
        if not tenant.instagram_page_access_token_encrypted:
            logger.warning("no instagram token for tenant %s", tenant.id)
            return
        try:
            from app.integrations.meta.client import InstagramClient
            client = await InstagramClient.create(tenant.id)
            await client.send_message(str(chat_id), text)
        except Exception as exc:
            logger.error(
                "failed to send instagram reply tenant=%s: %s", tenant.id, exc
            )


async def _write_error_audit(tenant_id: uuid.UUID, error: str) -> None:
    try:
        with tenant_context(tenant_id):
            async with get_tenant_session() as session:
                session.add(
                    AuditLog(
                        tenant_id=tenant_id,
                        action="agent_error",
                        entity_type="task",
                        entity_id=tenant_id,
                        payload_json={"error": error[:500]},
                    )
                )
                await session.commit()
    except Exception:
        logger.exception("failed to write error audit log for tenant %s", tenant_id)
