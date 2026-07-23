"""
Tests for cross-channel customer unification.

Flow simulated end-to-end (mocked DB):
  1.  Instagram DM arrives → CustomerService creates Customer(instagram_psid="IG_123")
  2.  ConversationAgent calls suggest_telegram_handoff → CustomerHandoff token minted
  3.  Deep link returned: t.me/TestBot?start=<token>
  4.  Customer taps link → Telegram /start <token>
  5.  Telegram webhook handler redeems token → Customer.telegram_chat_id = TG_CHAT_ID
  6.  Background task loads cross-channel history (IG messages visible on TG side)
  7.  Conversation continues seamlessly with full context

Individual units tested separately for clarity.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

TENANT_ID  = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CUSTOMER_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
HANDOFF_ID  = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
IG_PSID     = "IG_USER_123"
TG_CHAT_ID  = 987654321


# ── CustomerService unit tests ────────────────────────────────────────────────

def _make_customer(**kwargs) -> MagicMock:
    c = MagicMock()
    c.id = CUSTOMER_ID
    c.tenant_id = TENANT_ID
    c.instagram_psid = kwargs.get("instagram_psid")
    c.telegram_chat_id = kwargs.get("telegram_chat_id")
    c.phone = kwargs.get("phone")
    c.customer_name = kwargs.get("customer_name")
    return c


def _make_session(*, existing_customer=None, existing_handoff=None) -> AsyncMock:
    session = AsyncMock()

    async def execute(stmt):
        result = MagicMock()
        if existing_customer is not None:
            result.scalar_one_or_none.return_value = existing_customer
        elif existing_handoff is not None:
            result.scalar_one_or_none.return_value = existing_handoff
        else:
            result.scalar_one_or_none.return_value = None
        return result

    session.execute = execute
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


@pytest.mark.anyio
async def test_get_or_create_by_instagram_creates_new():
    from app.services.customer_service import CustomerService

    session = _make_session(existing_customer=None)
    cs = CustomerService(session)

    customer = await cs.get_or_create_by_instagram(
        tenant_id=TENANT_ID,
        instagram_psid=IG_PSID,
        name="Nodira",
    )

    session.add.assert_called_once()
    added = session.add.call_args[0][0]
    assert added.instagram_psid == IG_PSID
    assert added.customer_name == "Nodira"


@pytest.mark.anyio
async def test_get_or_create_by_instagram_returns_existing():
    from app.services.customer_service import CustomerService

    existing = _make_customer(instagram_psid=IG_PSID)
    session = _make_session(existing_customer=existing)
    cs = CustomerService(session)

    customer = await cs.get_or_create_by_instagram(
        tenant_id=TENANT_ID,
        instagram_psid=IG_PSID,
    )

    session.add.assert_not_called()
    assert customer is existing


@pytest.mark.anyio
async def test_get_or_create_by_telegram_creates_new():
    from app.services.customer_service import CustomerService

    session = _make_session(existing_customer=None)
    cs = CustomerService(session)

    await cs.get_or_create_by_telegram(
        tenant_id=TENANT_ID,
        telegram_chat_id=TG_CHAT_ID,
        name="Bobur",
    )

    session.add.assert_called_once()
    added = session.add.call_args[0][0]
    assert added.telegram_chat_id == TG_CHAT_ID


@pytest.mark.anyio
async def test_create_handoff_sets_expiry():
    from app.services.customer_service import CustomerService, _HANDOFF_TTL_MINUTES

    session = _make_session()
    cs = CustomerService(session)

    before = datetime.now(UTC)
    handoff = await cs.create_handoff(
        tenant_id=TENANT_ID,
        customer_id=CUSTOMER_ID,
    )
    after = datetime.now(UTC)

    session.add.assert_called_once()
    added = session.add.call_args[0][0]
    assert added.channel_from == "instagram"
    assert added.channel_to == "telegram"
    min_expiry = before + timedelta(minutes=_HANDOFF_TTL_MINUTES)
    max_expiry = after + timedelta(minutes=_HANDOFF_TTL_MINUTES)
    assert min_expiry <= added.expires_at <= max_expiry


# ── Handoff redemption ────────────────────────────────────────────────────────

def _make_handoff(
    *,
    expires_at: datetime | None = None,
    redeemed_at: datetime | None = None,
) -> MagicMock:
    h = MagicMock()
    h.id = HANDOFF_ID
    h.tenant_id = TENANT_ID
    h.customer_id = CUSTOMER_ID
    h.channel_from = "instagram"
    h.channel_to = "telegram"
    h.expires_at = expires_at or (datetime.now(UTC) + timedelta(minutes=5))
    h.redeemed_at = redeemed_at
    return h


@pytest.mark.anyio
async def test_redeem_handoff_links_telegram_identity():
    from app.services.customer_service import CustomerService

    customer = _make_customer(instagram_psid=IG_PSID, telegram_chat_id=None)
    handoff = _make_handoff()

    # Session needs to return handoff then customer on successive execute() calls
    session = AsyncMock()
    call_count = 0

    async def execute(stmt):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalar_one_or_none.return_value = handoff
        else:
            result.scalar_one_or_none.return_value = customer
        return result

    session.execute = execute
    session.add = MagicMock()
    session.flush = AsyncMock()

    cs = CustomerService(session)
    result = await cs.redeem_handoff(token=HANDOFF_ID, telegram_chat_id=TG_CHAT_ID)

    assert result is customer
    assert customer.telegram_chat_id == TG_CHAT_ID
    assert handoff.redeemed_at is not None


@pytest.mark.anyio
async def test_redeem_handoff_expired_returns_none():
    from app.services.customer_service import CustomerService

    handoff = _make_handoff(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    session = _make_session(existing_handoff=handoff)
    cs = CustomerService(session)

    result = await cs.redeem_handoff(token=HANDOFF_ID, telegram_chat_id=TG_CHAT_ID)

    assert result is None
    assert handoff.redeemed_at is None  # Not marked used


@pytest.mark.anyio
async def test_redeem_handoff_already_used_returns_none():
    from app.services.customer_service import CustomerService

    used_at = datetime.now(UTC) - timedelta(minutes=2)
    handoff = _make_handoff(redeemed_at=used_at)
    session = _make_session(existing_handoff=handoff)
    cs = CustomerService(session)

    result = await cs.redeem_handoff(token=HANDOFF_ID, telegram_chat_id=TG_CHAT_ID)

    assert result is None


@pytest.mark.anyio
async def test_redeem_handoff_not_found_returns_none():
    from app.services.customer_service import CustomerService

    session = _make_session(existing_handoff=None)
    cs = CustomerService(session)

    result = await cs.redeem_handoff(token=uuid.uuid4(), telegram_chat_id=TG_CHAT_ID)

    assert result is None


# ── Cross-channel history ─────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_cross_channel_history_returns_oldest_first():
    from app.services.customer_service import CustomerService
    from app.db.models.conversation import Message

    # Build fake message rows: (Message, channel), newest-first from DB
    def _msg(role, content, seconds_ago):
        m = MagicMock(spec=Message)
        m.role = role
        m.content = content
        m.created_at = datetime.now(UTC) - timedelta(seconds=seconds_ago)
        return m

    rows = [
        (_msg("assistant", "Salom!", 10), "instagram"),
        (_msg("user", "Narx bormi?", 30), "instagram"),
        (_msg("user", "Salom", 60), "instagram"),
    ]

    session = AsyncMock()

    async def execute(stmt):
        result = MagicMock()
        result.all.return_value = rows
        return result

    session.execute = execute
    cs = CustomerService(session)

    history = await cs.get_cross_channel_history(
        tenant_id=TENANT_ID,
        customer_id=CUSTOMER_ID,
        last_n=10,
    )

    # Should be reversed to oldest-first
    assert len(history) == 3
    assert history[0]["content"] == "Salom"     # oldest
    assert history[1]["content"] == "Narx bormi?"
    assert history[2]["content"] == "Salom!"    # newest


# ── ConversationAgent handoff tool ───────────────────────────────────────────

def _make_conv_agent(config_json: dict | None = None):
    from app.agents.conversation import ConversationAgent
    rec = MagicMock()
    rec.id = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    rec.type = "conversation"
    rec.name = "Conversation"
    rec.system_prompt = None
    rec.config_json = config_json or {}
    rec.autonomy_level = 2
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    return ConversationAgent(
        tenant_id=TENANT_ID,
        agent_db_record=rec,
        anthropic_client=MagicMock(),
        db_session=session,
    )


@pytest.mark.anyio
async def test_handoff_tool_creates_token_when_customer_id_set():
    """When _active_customer_id is set, the tool creates a handoff and embeds token in link."""
    from app.services.customer_service import CustomerService

    agent = _make_conv_agent(config_json={"telegram_bot_username": "@AlphaBot"})
    agent._active_customer_id = str(CUSTOMER_ID)

    fake_handoff = MagicMock()
    fake_handoff.id = HANDOFF_ID

    mock_cs = AsyncMock(spec=CustomerService)
    mock_cs.create_handoff = AsyncMock(return_value=fake_handoff)

    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.services.customer_service.CustomerService", return_value=mock_cs
    ):
        result = await agent._tool_suggest_telegram_handoff({"reason": "bulk order"})

    assert str(HANDOFF_ID) in result["message"]
    assert "t.me/AlphaBot" in result["message"]
    assert result["token"] == str(HANDOFF_ID)
    assert result["reason"] == "bulk order"


@pytest.mark.anyio
async def test_handoff_tool_no_token_when_no_customer_id():
    """Without customer_id, returns static deep link without token."""
    agent = _make_conv_agent(config_json={"telegram_bot_username": "@AlphaBot"})
    agent._active_customer_id = None

    result = await agent._tool_suggest_telegram_handoff({"reason": "bulk order"})

    assert result["token"] is None
    assert "t.me/AlphaBot" in result["message"]
    assert "?start=" not in result["message"]


@pytest.mark.anyio
async def test_handoff_tool_no_username_no_link():
    """Without bot username, returns a generic message with no link."""
    agent = _make_conv_agent()  # no telegram_bot_username
    agent._active_customer_id = None

    result = await agent._tool_suggest_telegram_handoff({"reason": "bulk"})

    assert "Telegram" in result["message"]
    assert "t.me" not in result["message"]


# ── Telegram /start token extraction ─────────────────────────────────────────

def test_extract_start_token_valid():
    from app.integrations.telegram.webhook_handler import _extract_start_token
    token = uuid.uuid4()
    result = _extract_start_token(f"/start {token}")
    assert result == token


def test_extract_start_token_plain_start():
    from app.integrations.telegram.webhook_handler import _extract_start_token
    assert _extract_start_token("/start") is None


def test_extract_start_token_invalid_uuid():
    from app.integrations.telegram.webhook_handler import _extract_start_token
    assert _extract_start_token("/start not-a-uuid") is None


def test_extract_start_token_regular_message():
    from app.integrations.telegram.webhook_handler import _extract_start_token
    assert _extract_start_token("Salom, narx bormi?") is None


# ── Full flow: IG → token → TG /start → merged history ───────────────────────

@pytest.mark.anyio
async def test_full_cross_channel_handoff_flow():
    """
    Simulate the complete handoff journey without any I/O:
      1. Instagram DM → Customer created
      2. Agent suggests Telegram → handoff token minted
      3. /start <token> on Telegram → identity merged
      4. Cross-channel history includes Instagram messages
    """
    from app.services.customer_service import CustomerService

    # ── Step 1: Instagram DM → customer created ──────────────────────────────
    ig_customer = _make_customer(instagram_psid=IG_PSID)
    ig_session = _make_session(existing_customer=None)

    cs = CustomerService(ig_session)
    # Simulate customer creation
    ig_session.add.side_effect = lambda obj: setattr(obj, "id", CUSTOMER_ID)
    _ = await cs.get_or_create_by_instagram(
        tenant_id=TENANT_ID, instagram_psid=IG_PSID, name="Nodira"
    )
    ig_session.add.assert_called_once()

    # ── Step 2: Agent creates handoff token ──────────────────────────────────
    agent = _make_conv_agent(config_json={"telegram_bot_username": "@AlphaBot"})
    agent._active_customer_id = str(CUSTOMER_ID)

    fake_handoff = MagicMock()
    fake_handoff.id = HANDOFF_ID

    mock_cs = AsyncMock(spec=CustomerService)
    mock_cs.create_handoff = AsyncMock(return_value=fake_handoff)

    with __import__("unittest.mock", fromlist=["patch"]).patch(
        "app.services.customer_service.CustomerService", return_value=mock_cs
    ):
        handoff_result = await agent._tool_suggest_telegram_handoff({"reason": "bulk order"})

    token = handoff_result["token"]
    assert token == str(HANDOFF_ID)
    deep_link = handoff_result["message"]
    assert f"t.me/AlphaBot?start={token}" in deep_link

    # ── Step 3: /start <token> on Telegram → identity merged ────────────────
    tg_customer = _make_customer(instagram_psid=IG_PSID, telegram_chat_id=None)
    valid_handoff = _make_handoff()
    tg_session = AsyncMock()
    call_count = 0

    async def tg_execute(stmt):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.scalar_one_or_none.return_value = valid_handoff
        else:
            result.scalar_one_or_none.return_value = tg_customer
        return result

    tg_session.execute = tg_execute
    tg_session.add = MagicMock()
    tg_session.flush = AsyncMock()

    cs2 = CustomerService(tg_session)
    merged = await cs2.redeem_handoff(
        token=HANDOFF_ID,
        telegram_chat_id=TG_CHAT_ID,
    )

    assert merged is tg_customer
    assert tg_customer.telegram_chat_id == TG_CHAT_ID
    assert valid_handoff.redeemed_at is not None

    # ── Step 4: Cross-channel history includes IG messages ───────────────────
    ig_message = MagicMock()
    ig_message.role = "user"
    ig_message.content = "Ko'rpa bormi?"
    ig_message.created_at = datetime.now(UTC) - timedelta(minutes=5)

    ig_reply = MagicMock()
    ig_reply.role = "assistant"
    ig_reply.content = "Ha, albatta! Qaysi o'lchamda?"
    ig_reply.created_at = datetime.now(UTC) - timedelta(minutes=4)

    history_session = AsyncMock()

    async def history_execute(stmt):
        result = MagicMock()
        result.all.return_value = [
            (ig_reply, "instagram"),   # newest first from DB
            (ig_message, "instagram"),
        ]
        return result

    history_session.execute = history_execute
    cs3 = CustomerService(history_session)

    history = await cs3.get_cross_channel_history(
        tenant_id=TENANT_ID,
        customer_id=CUSTOMER_ID,
        last_n=10,
    )

    # Oldest-first, both IG messages visible from TG side
    assert len(history) == 2
    assert history[0]["content"] == "Ko'rpa bormi?"       # oldest
    assert history[1]["content"] == "Ha, albatta! Qaysi o'lchamda?"
    assert history[0]["role"] == "user"
    assert history[1]["role"] == "assistant"
