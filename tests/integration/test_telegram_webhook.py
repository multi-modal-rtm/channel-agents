"""
Integration test: Telegram webhook → message stored → background task
→ orchestrator + ConversationAgent → reply stored + sent via Telegram.

No Redis required: the task function is called directly.
TenantAwareAnthropicClient.create is mocked so no real Anthropic key is needed,
but the mocked chat() returns responses with non-zero usage so the audit_log
path still exercises the action-logging code.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text

from tests.integration.conftest import TENANT_A_ID

TENANT_SLUG = "alpha-int"
CUSTOMER_TG_ID = 9876543
WEBHOOK_SECRET = "test-webhook-secret"
FAKE_BOT_TOKEN = "1234567890:ABCDEFtest"

TELEGRAM_UPDATE = {
    "update_id": 100001,
    "message": {
        "message_id": 42,
        "from": {
            "id": CUSTOMER_TG_ID,
            "is_bot": False,
            "first_name": "Alisher",
            "last_name": "Karimov",
            "language_code": "uz",
        },
        "chat": {"id": CUSTOMER_TG_ID, "type": "private", "first_name": "Alisher"},
        "date": 1700000000,
        "text": "ko'rpa narxi qancha?",
    },
}


# ── Fake Anthropic response helpers ──────────────────────────────────────────

def _fake_text_block(text_content: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text_content
    return block


def _fake_routing_block(specialist: str, task: str) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = "route_to_specialist"
    block.id = f"tool_{uuid.uuid4().hex[:8]}"
    block.input = {"specialist": specialist, "task": task}
    return block


def _fake_anthropic_response(content_blocks: list, *, stop_reason: str = "end_turn") -> MagicMock:
    resp = MagicMock()
    resp.content = content_blocks
    resp.stop_reason = stop_reason
    resp.usage = MagicMock(input_tokens=200, output_tokens=50)
    return resp


def _make_mock_anthropic_client(*responses) -> AsyncMock:
    """Return a mock TenantAwareAnthropicClient whose chat() yields responses in order."""
    mock_client = AsyncMock()
    mock_client.chat = AsyncMock(side_effect=list(responses))
    return mock_client


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def seeded_tenant(test_session_factory):
    """Insert agent records and telegram config for tenant A."""
    from cryptography.fernet import Fernet
    from app.config import settings
    fernet = Fernet(settings.encryption_key.encode())

    encrypted_token = fernet.encrypt(FAKE_BOT_TOKEN.encode())
    encrypted_secret = fernet.encrypt(WEBHOOK_SECRET.encode())

    from app.db.models.agent import Agent
    from app.db.models.product import Product

    async with test_session_factory() as session:
        await session.execute(
            text("""
                UPDATE tenants
                SET telegram_bot_token_encrypted = :token,
                    telegram_webhook_secret = :secret
                WHERE id = :tid
            """),
            {
                "token": encrypted_token,
                "secret": encrypted_secret,
                "tid": str(TENANT_A_ID),
            },
        )

        for agent_type, agent_name, autonomy, model in [
            ("orchestrator",   "Orchestrator",   2, "claude-opus-4-7"),
            ("conversation",   "Conversation",   2, "claude-sonnet-4-6"),
            ("content",        "Content",        1, "claude-sonnet-4-6"),
            ("lead_qualifier", "Lead Qualifier", 2, "claude-haiku-4-5"),
        ]:
            exists = await session.execute(
                select(Agent).where(
                    Agent.tenant_id == TENANT_A_ID,
                    Agent.type == agent_type,
                )
            )
            if exists.scalar_one_or_none() is None:
                session.add(Agent(
                    tenant_id=TENANT_A_ID,
                    type=agent_type,
                    name=agent_name,
                    autonomy_level=autonomy,
                    config_json={"model": model},
                    enabled=True,
                ))

        exists = await session.execute(
            select(Product).where(
                Product.tenant_id == TENANT_A_ID,
                Product.sku == "KORPA-TEST",
            )
        )
        if exists.scalar_one_or_none() is None:
            session.add(Product(
                tenant_id=TENANT_A_ID,
                sku="KORPA-TEST",
                name_uz="Ko'rpa 175x215",
                name_ru="Одеяло 175x215",
                description="Test ko'rpa",
                price_uzs=420_000,
                in_stock=True,
            ))

        await session.commit()

    yield TENANT_A_ID


# ── Tests ─────────────────────────────────────────────────────────────────────

async def test_telegram_webhook_stores_message_and_conversation(
    client: AsyncClient,
    seeded_tenant,
    test_session_factory,
):
    """POST to webhook → conversation + user message rows created in DB."""
    resp = await client.post(
        f"/api/v1/webhooks/telegram/{TENANT_SLUG}",
        json=TELEGRAM_UPDATE,
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )
    assert resp.status_code == 200

    from app.db.models.conversation import Conversation, Message
    async with test_session_factory() as session:
        conv = await session.execute(
            select(Conversation).where(
                Conversation.tenant_id == TENANT_A_ID,
                Conversation.channel == "telegram",
                Conversation.customer_handle == str(CUSTOMER_TG_ID),
            )
        )
        conv_row = conv.scalar_one_or_none()
        assert conv_row is not None, "conversation should have been created"

        msgs = await session.execute(
            select(Message).where(
                Message.tenant_id == TENANT_A_ID,
                Message.conversation_id == conv_row.id,
                Message.role == "user",
            )
        )
        msg_rows = msgs.scalars().all()
        assert len(msg_rows) >= 1
        assert any("ko'rpa" in m.content for m in msg_rows)


async def test_background_task_calls_orchestrator_and_stores_reply(
    client: AsyncClient,
    seeded_tenant,
    test_session_factory,
):
    """Call the background task directly; verify reply stored + Telegram notified."""
    await client.post(
        f"/api/v1/webhooks/telegram/{TENANT_SLUG}",
        json=TELEGRAM_UPDATE,
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )

    from app.db.models.conversation import Conversation, Message
    async with test_session_factory() as session:
        conv_result = await session.execute(
            select(Conversation).where(
                Conversation.tenant_id == TENANT_A_ID,
                Conversation.channel == "telegram",
                Conversation.customer_handle == str(CUSTOMER_TG_ID),
            )
        )
        conv_row = conv_result.scalar_one()

    REPLY_TEXT = "Ko'rpalarimiz narxi 220 000 so'mdan boshlanadi!"
    orch_resp = _fake_anthropic_response(
        [_fake_routing_block("conversation", "Customer asks about ko'rpa price")],
        stop_reason="tool_use",
    )
    conv_resp = _fake_anthropic_response(
        [_fake_text_block(REPLY_TEXT)],
        stop_reason="end_turn",
    )

    mock_llm = _make_mock_anthropic_client(orch_resp, conv_resp)

    with (
        patch(
            "app.workers.tasks.handle_incoming_message.TenantAwareAnthropicClient.create",
            new_callable=AsyncMock,
            return_value=mock_llm,
        ),
        patch(
            "app.integrations.telegram.client.TelegramClient.send_message",
            new_callable=AsyncMock,
        ) as mock_send,
    ):
        from app.workers.tasks.handle_incoming_message import handle_incoming_message
        await handle_incoming_message(
            {},
            tenant_id=str(TENANT_A_ID),
            conversation_id=str(conv_row.id),
            message_text="ko'rpa narxi qancha?",
            channel="telegram",
            chat_id=CUSTOMER_TG_ID,
        )

    async with test_session_factory() as session:
        reply_result = await session.execute(
            select(Message).where(
                Message.tenant_id == TENANT_A_ID,
                Message.conversation_id == conv_row.id,
                Message.role == "assistant",
            )
        )
        replies = reply_result.scalars().all()
        assert len(replies) >= 1

    mock_send.assert_called_once()
    assert mock_send.call_args[0][0] == CUSTOMER_TG_ID
    sent_text = mock_send.call_args[0][1]
    assert REPLY_TEXT in sent_text or "220 000" in sent_text or "Ko'rpa" in sent_text


async def test_background_task_writes_audit_log(
    client: AsyncClient,
    seeded_tenant,
    test_session_factory,
):
    """Background task must write at least one audit_log row for the tenant."""
    update2 = {
        **TELEGRAM_UPDATE,
        "update_id": 100002,
        "message": {**TELEGRAM_UPDATE["message"], "message_id": 43, "text": "yostiq bormi?"},
    }
    await client.post(
        f"/api/v1/webhooks/telegram/{TENANT_SLUG}",
        json=update2,
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )

    from app.db.models.conversation import Conversation
    async with test_session_factory() as session:
        conv_result = await session.execute(
            select(Conversation).where(
                Conversation.tenant_id == TENANT_A_ID,
                Conversation.channel == "telegram",
                Conversation.customer_handle == str(CUSTOMER_TG_ID),
            )
        )
        conv_row = conv_result.scalar_one()

    orch_resp = _fake_anthropic_response(
        [_fake_routing_block("conversation", "Customer asks about yostiq")],
        stop_reason="tool_use",
    )
    conv_resp = _fake_anthropic_response(
        [_fake_text_block("Ha, yostiqlarimiz bor!")],
        stop_reason="end_turn",
    )
    mock_llm = _make_mock_anthropic_client(orch_resp, conv_resp)

    with (
        patch(
            "app.workers.tasks.handle_incoming_message.TenantAwareAnthropicClient.create",
            new_callable=AsyncMock,
            return_value=mock_llm,
        ),
        patch(
            "app.integrations.telegram.client.TelegramClient.send_message",
            new_callable=AsyncMock,
        ),
    ):
        from app.workers.tasks.handle_incoming_message import handle_incoming_message
        await handle_incoming_message(
            {},
            tenant_id=str(TENANT_A_ID),
            conversation_id=str(conv_row.id),
            message_text="yostiq bormi?",
            channel="telegram",
            chat_id=CUSTOMER_TG_ID,
        )

    from app.db.models.audit_log import AuditLog
    async with test_session_factory() as session:
        logs = await session.execute(
            select(AuditLog).where(AuditLog.tenant_id == TENANT_A_ID)
        )
        rows = logs.scalars().all()
        assert len(rows) > 0
        actions = {r.action for r in rows}
        assert any("handle" in a for a in actions)


async def test_webhook_invalid_secret_returns_403(
    client: AsyncClient,
    seeded_tenant,
):
    """Wrong webhook secret must be rejected with 403."""
    resp = await client.post(
        f"/api/v1/webhooks/telegram/{TENANT_SLUG}",
        json=TELEGRAM_UPDATE,
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
    )
    assert resp.status_code == 403


async def test_webhook_unknown_slug_returns_200(client: AsyncClient, seeded_tenant):
    """Unknown tenant slug must return 200 (avoids Telegram retry storms)."""
    resp = await client.post(
        "/api/v1/webhooks/telegram/no-such-tenant",
        json=TELEGRAM_UPDATE,
    )
    assert resp.status_code == 200
