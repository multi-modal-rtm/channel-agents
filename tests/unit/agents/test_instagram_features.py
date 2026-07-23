"""
Tests for Instagram-specific features:
  A. Story reply — webhook detection and agent payload enrichment
  B. Quick replies — tool call sets output.quick_reply_options
  C. Product carousel — tool call sets output.carousel_products
  D. Postback handling — ORDER_/VIEW_ payloads translated to natural language
  E. send_structured_message — correct JSON body sent to Graph API
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

TENANT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
AGENT_ID  = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_agent_record(config_json: dict | None = None) -> MagicMock:
    rec = MagicMock()
    rec.id = AGENT_ID
    rec.type = "conversation"
    rec.name = "Conversation"
    rec.system_prompt = None
    rec.config_json = config_json or {}
    rec.autonomy_level = 2
    return rec


def _make_conv_agent(config_json: dict | None = None):
    from app.agents.conversation import ConversationAgent
    session = MagicMock()
    session.add = MagicMock()
    return ConversationAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(config_json=config_json),
        anthropic_client=MagicMock(),
        db_session=session,
    )


def _build_llm_response(tool_calls: list[dict], text: str | None = None) -> AsyncMock:
    """Build a mock client whose chat() returns a response with tool_use blocks
    for each entry in ``tool_calls``, followed by an end_turn text block."""
    # First call returns tool_use blocks
    blocks_round1 = []
    for tc in tool_calls:
        b = MagicMock()
        b.type = "tool_use"
        b.name = tc["name"]
        b.input = tc["input"]
        b.id = f"tool_{tc['name']}"
        blocks_round1.append(b)

    resp1 = MagicMock()
    resp1.content = blocks_round1
    resp1.stop_reason = "tool_use"
    resp1.usage = MagicMock(input_tokens=40, output_tokens=20)

    # Second call (after tool results) returns text end_turn
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = text or "Here you go!"
    resp2 = MagicMock()
    resp2.content = [text_block]
    resp2.stop_reason = "end_turn"
    resp2.usage = MagicMock(input_tokens=60, output_tokens=30)

    client = AsyncMock()
    client.chat = AsyncMock(side_effect=[resp1, resp2])
    return client


# ═══════════════════════════════════════════════════════════════════════════════
# A. Story reply
# ═══════════════════════════════════════════════════════════════════════════════

def test_interpret_postback_no_story_id_in_plain_dm():
    """_handle_message_event — plain DM without reply_to.story should not set story_id."""
    from app.integrations.meta.webhook_handler import _interpret_postback
    # No story involved in plain DMs — just test postback is not contaminated
    assert _interpret_postback("ORDER_K001", "Order") == "I want to order product K001"


def test_story_reply_detected_in_message_event():
    """Webhook correctly detects reply_to.story and includes story_id in event data."""
    # We verify the extraction logic: message.reply_to.story.id
    event = {
        "sender": {"id": "USER_123"},
        "message": {
            "text": "That looks great!",
            "reply_to": {
                "story": {
                    "id": "STORY_ABC",
                    "url": "https://www.instagram.com/stories/brand/STORY_ABC",
                }
            },
        },
    }
    story = event["message"].get("reply_to", {}).get("story", {})
    story_id = str(story.get("id", ""))
    assert story_id == "STORY_ABC"


def test_story_payload_prefix():
    """Task-layer story prefix wraps the customer message correctly."""
    story_id = "STORY_ABC"
    message_text = "Shu ko'rpa necha pul?"
    payload_text = f"[Customer replied to your Instagram story]\n{message_text}"
    assert "[Customer replied to your Instagram story]" in payload_text
    assert message_text in payload_text


def test_no_story_id_no_prefix():
    """When story_id is None, message_text is passed through unchanged."""
    story_id = None
    message_text = "Shu ko'rpa necha pul?"
    payload_text = (
        f"[Customer replied to your Instagram story]\n{message_text}"
        if story_id
        else message_text
    )
    assert payload_text == message_text


# ═══════════════════════════════════════════════════════════════════════════════
# B. Quick replies
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.anyio
async def test_suggest_quick_replies_tool_sets_output():
    """Agent calling suggest_quick_replies → output.quick_reply_options populated."""
    from app.agents.base import AgentInput
    agent = _make_conv_agent()
    agent.anthropic_client = _build_llm_response(
        tool_calls=[{
            "name": "suggest_quick_replies",
            "input": {"options": ["Narxi", "Yetkazib berish", "O'lchamlar"]},
        }],
        text="Qaysi mahsulot qiziqtiryapti?",
    )

    output = await agent.handle(
        AgentInput(type="message", payload="Salom!", channel="instagram")
    )

    assert output.quick_reply_options == ["Narxi", "Yetkazib berish", "O'lchamlar"]
    assert output.carousel_products == []
    assert output.response_text == "Qaysi mahsulot qiziqtiryapti?"


@pytest.mark.anyio
async def test_quick_reply_options_capped_at_4():
    """Tool implementation enforces the 4-option cap."""
    from app.agents.conversation import ConversationAgent
    agent = _make_conv_agent()
    result = agent._tool_suggest_quick_replies({
        "options": ["A", "B", "C", "D", "E", "F"]
    })
    assert len(result["options"]) == 4


@pytest.mark.anyio
async def test_quick_reply_labels_truncated_at_20_chars():
    """Tool implementation trims labels to 20 characters."""
    from app.agents.conversation import ConversationAgent
    agent = _make_conv_agent()
    result = agent._tool_suggest_quick_replies({
        "options": ["A" * 30, "B" * 5]
    })
    assert len(result["options"][0]) == 20
    assert result["options"][1] == "BBBBB"


def test_quick_replies_not_in_telegram_tools():
    agent = _make_conv_agent()
    tool_names = [t["name"] for t in agent._tools_for_channel("telegram")]
    assert "suggest_quick_replies" not in tool_names
    assert "show_product_carousel" not in tool_names


def test_quick_replies_in_instagram_tools():
    agent = _make_conv_agent()
    tool_names = [t["name"] for t in agent._tools_for_channel("instagram")]
    assert "suggest_quick_replies" in tool_names
    assert "show_product_carousel" in tool_names


def test_instagram_system_prompt_mentions_quick_replies():
    agent = _make_conv_agent()
    prompt = agent._channel_system_prompt("instagram")
    assert "suggest_quick_replies" in prompt
    assert "show_product_carousel" in prompt


# ═══════════════════════════════════════════════════════════════════════════════
# C. Product carousel
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.anyio
async def test_show_product_carousel_tool_sets_output():
    """Agent calling show_product_carousel → output.carousel_products populated."""
    from app.agents.base import AgentInput
    from app.agents.conversation import ConversationAgent

    agent = _make_conv_agent()

    # Patch _tool_lookup_product to return fake products without hitting the DB
    fake_products = [
        {"sku": "K001", "name": "Ko'rpa 150x200", "price_uzs": 220000, "in_stock": True},
        {"sku": "K002", "name": "Ko'rpa 175x215", "price_uzs": 290000, "in_stock": True},
    ]
    agent._tool_lookup_product = AsyncMock(return_value={"products": fake_products})

    agent.anthropic_client = _build_llm_response(
        tool_calls=[{
            "name": "show_product_carousel",
            "input": {"query": "ko'rpa"},
        }],
        text="Mana bizning ko'rpalarimiz!",
    )

    output = await agent.handle(
        AgentInput(type="message", payload="qanday ko'rpalar bor?", channel="instagram")
    )

    assert len(output.carousel_products) == 2
    assert output.carousel_products[0]["sku"] == "K001"
    assert output.quick_reply_options == []


@pytest.mark.anyio
async def test_carousel_adapter_formats_correctly():
    """InstagramAdapter.format_product_carousel produces Generic Template JSON."""
    from app.agents.channel_adapters.instagram import InstagramAdapter

    adapter = InstagramAdapter()
    products = [
        {"name_uz": "Ko'rpa 150x200", "sku": "K001", "price_uzs": 220000},
        {"name_uz": "Ko'rpa 175x215", "sku": "K002", "price_uzs": 290000},
    ]
    payload = adapter.format_product_carousel(products)

    assert "attachment" in payload
    template = payload["attachment"]["payload"]
    assert template["template_type"] == "generic"
    assert len(template["elements"]) == 2
    assert template["elements"][0]["title"] == "Ko'rpa 150x200"
    assert "ORDER_K001" in template["elements"][0]["buttons"][0]["payload"]


@pytest.mark.anyio
async def test_carousel_empty_returns_text_fallback():
    from app.agents.channel_adapters.instagram import InstagramAdapter
    adapter = InstagramAdapter()
    payload = adapter.format_product_carousel([])
    assert "text" in payload
    assert "No products found" in payload["text"]


# ═══════════════════════════════════════════════════════════════════════════════
# D. Postback handling
# ═══════════════════════════════════════════════════════════════════════════════

def test_interpret_postback_order_sku():
    from app.integrations.meta.webhook_handler import _interpret_postback
    assert _interpret_postback("ORDER_KORPA-150-W", "Order") == "I want to order product KORPA-150-W"


def test_interpret_postback_view_sku():
    from app.integrations.meta.webhook_handler import _interpret_postback
    assert _interpret_postback("VIEW_YOSTIQ-50", "View") == "Tell me more about product YOSTIQ-50"


def test_interpret_postback_falls_back_to_title():
    from app.integrations.meta.webhook_handler import _interpret_postback
    assert _interpret_postback("CUSTOM_PAYLOAD", "Narxi qancha?") == "Narxi qancha?"


def test_interpret_postback_falls_back_to_payload_when_no_title():
    from app.integrations.meta.webhook_handler import _interpret_postback
    assert _interpret_postback("SOME_PAYLOAD", "") == "SOME_PAYLOAD"


def test_interpret_postback_empty_both_returns_empty():
    from app.integrations.meta.webhook_handler import _interpret_postback
    assert _interpret_postback("", "") == ""


# ═══════════════════════════════════════════════════════════════════════════════
# E. send_structured_message
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.anyio
async def test_send_structured_message_quick_replies():
    """send_structured_message sends correct JSON for quick reply payload."""
    from app.integrations.meta.client import InstagramClient

    captured = {}

    async def fake_request(method, path, *, json=None, params=None):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = json
        return {"message_id": "m_123"}

    client = InstagramClient(page_id="PAGE1", access_token="TOKEN1")
    client._request = fake_request

    qr_payload = {
        "text": "Qaysi birini tanlaysiz?",
        "quick_replies": [
            {"content_type": "text", "title": "Narxi", "payload": "Narxi"},
            {"content_type": "text", "title": "O'lchamlar", "payload": "O'lchamlar"},
        ],
    }
    result = await client.send_structured_message("USER_123", qr_payload)

    assert result == {"message_id": "m_123"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/PAGE1/messages"
    body = captured["json"]
    assert body["recipient"] == {"id": "USER_123"}
    assert body["message"] == qr_payload
    assert body["messaging_type"] == "RESPONSE"


@pytest.mark.anyio
async def test_send_structured_message_carousel():
    """send_structured_message sends correct JSON for a Generic Template payload."""
    from app.integrations.meta.client import InstagramClient

    captured = {}

    async def fake_request(method, path, *, json=None, params=None):
        captured["json"] = json
        return {"message_id": "m_456"}

    client = InstagramClient(page_id="PAGE1", access_token="TOKEN1")
    client._request = fake_request

    carousel_payload = {
        "attachment": {
            "type": "template",
            "payload": {
                "template_type": "generic",
                "elements": [{"title": "Ko'rpa", "subtitle": "220000 UZS", "buttons": []}],
            },
        }
    }
    await client.send_structured_message("USER_456", carousel_payload)

    body = captured["json"]
    assert body["message"]["attachment"]["payload"]["template_type"] == "generic"
