"""
Tests for channel adapters and the ConversationAgent channel-awareness.

Split into three sections:
  A. Adapter unit tests — pure formatting logic, no mocks needed
  B. Agent channel routing — tools and system prompt differ by channel
  C. Integration — same agent input, different formatted output per channel
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.channel_adapters import get_adapter
from app.agents.channel_adapters.instagram import InstagramAdapter, _strip_markdown
from app.agents.channel_adapters.telegram import TelegramAdapter

TENANT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
AGENT_ID  = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


# ═══════════════════════════════════════════════════════════════════════════════
# A. Adapter unit tests
# ═══════════════════════════════════════════════════════════════════════════════

# ── TelegramAdapter ───────────────────────────────────────────────────────────

def test_telegram_max_length_is_4096():
    assert TelegramAdapter().max_length == 4096


def test_telegram_preserves_bold_markdown():
    adapter = TelegramAdapter()
    text = "Here is **bold** and _italic_ and `code`."
    assert adapter.format_message(text) == text


def test_telegram_truncates_long_message():
    adapter = TelegramAdapter()
    long_text = "a" * 5000
    result = adapter.format_message(long_text)
    assert len(result) == 4096
    assert result.endswith("…")


def test_telegram_short_message_unchanged():
    adapter = TelegramAdapter()
    text = "Hello, how can I help you today?"
    assert adapter.format_message(text) == text


def test_telegram_product_carousel_uses_markdown():
    adapter = TelegramAdapter()
    products = [
        {"name_uz": "Ko'rpa", "sku": "K001", "price_uzs": 150000},
        {"name_uz": "Yostiq", "sku": "Y001", "price_uzs": 50000},
    ]
    payload = adapter.format_product_carousel(products)
    assert "Ko'rpa" in payload["text"]
    assert "*Ko'rpa*" in payload["text"]  # Telegram uses bold


# ── InstagramAdapter ──────────────────────────────────────────────────────────

def test_instagram_max_length_is_1000():
    assert InstagramAdapter().max_length == 1000


def test_instagram_strips_bold_asterisk():
    assert _strip_markdown("Hello **world**") == "Hello world"


def test_instagram_strips_bold_underscore():
    assert _strip_markdown("Hello __world__") == "Hello world"


def test_instagram_strips_italic_asterisk():
    assert _strip_markdown("Hello *world*") == "Hello world"


def test_instagram_strips_italic_underscore():
    assert _strip_markdown("_italic text_") == "italic text"


def test_instagram_strips_strikethrough():
    assert _strip_markdown("~~deleted~~") == "deleted"


def test_instagram_strips_inline_code():
    assert _strip_markdown("`code snippet`") == "code snippet"


def test_instagram_strips_code_block():
    result = _strip_markdown("```\nsome code\n```")
    assert "```" not in result
    assert "some code" in result


def test_instagram_strips_h1_header():
    result = _strip_markdown("# Big Heading\nContent below")
    assert "# Big Heading" not in result
    assert "Big Heading" in result


def test_instagram_strips_h3_header():
    result = _strip_markdown("### Section\nText")
    assert "###" not in result


def test_instagram_converts_link_to_text():
    result = _strip_markdown("[Click here](https://example.com)")
    assert "Click here" in result
    assert "https://example.com" not in result


def test_instagram_format_message_strips_and_truncates():
    adapter = InstagramAdapter()
    text = "**Bold** product: _Ko'rpa_ — only `150 000` UZS!"
    result = adapter.format_message(text)
    assert "**" not in result
    assert "_" not in result
    assert "`" not in result
    assert "Ko'rpa" in result
    assert len(result) <= 1000


def test_instagram_truncates_at_1000():
    adapter = InstagramAdapter()
    long_text = "b" * 1500
    result = adapter.format_message(long_text)
    assert len(result) == 1000
    assert result.endswith("…")


def test_instagram_quick_replies_capped_at_13():
    adapter = InstagramAdapter()
    options = [f"Option {i}" for i in range(20)]
    payload = adapter.format_quick_replies("Choose one:", options)
    assert len(payload["quick_replies"]) == 13


def test_instagram_quick_replies_title_truncated():
    adapter = InstagramAdapter()
    long_option = "This option title is way too long for a quick reply button"
    payload = adapter.format_quick_replies("Pick:", [long_option])
    title = payload["quick_replies"][0]["title"]
    assert len(title) <= 20


def test_instagram_quick_replies_structure():
    adapter = InstagramAdapter()
    payload = adapter.format_quick_replies("Which size?", ["Small", "Medium", "Large"])
    assert payload["text"] == "Which size?"
    assert payload["quick_replies"][0]["content_type"] == "text"
    assert payload["quick_replies"][0]["payload"] == "Small"


def test_instagram_product_carousel_generic_template():
    adapter = InstagramAdapter()
    products = [
        {"name_uz": "Ko'rpa", "sku": "K001", "price_uzs": 150000},
        {"name_uz": "Yostiq", "sku": "Y001", "price_uzs": 50000},
    ]
    payload = adapter.format_product_carousel(products)
    assert "attachment" in payload
    template = payload["attachment"]["payload"]
    assert template["template_type"] == "generic"
    assert len(template["elements"]) == 2
    assert template["elements"][0]["title"] == "Ko'rpa"
    assert "ORDER_K001" in template["elements"][0]["buttons"][0]["payload"]


def test_instagram_product_carousel_empty_returns_text():
    adapter = InstagramAdapter()
    payload = adapter.format_product_carousel([])
    assert "text" in payload
    assert "No products found" in payload["text"]


def test_instagram_product_carousel_caps_at_10():
    adapter = InstagramAdapter()
    products = [{"name_uz": f"Product {i}", "sku": f"P{i:03d}", "price_uzs": 10000} for i in range(20)]
    payload = adapter.format_product_carousel(products)
    assert len(payload["attachment"]["payload"]["elements"]) == 10


# ── Factory function ──────────────────────────────────────────────────────────

def test_get_adapter_returns_instagram_for_instagram():
    assert isinstance(get_adapter("instagram"), InstagramAdapter)


def test_get_adapter_returns_instagram_for_instagram_comment():
    assert isinstance(get_adapter("instagram_comment"), InstagramAdapter)


def test_get_adapter_returns_telegram_for_telegram():
    assert isinstance(get_adapter("telegram"), TelegramAdapter)


def test_get_adapter_defaults_to_telegram_for_unknown():
    assert isinstance(get_adapter("whatsapp"), TelegramAdapter)


# ═══════════════════════════════════════════════════════════════════════════════
# B. Agent channel routing
# ═══════════════════════════════════════════════════════════════════════════════

def _make_agent_record(*, config_json: dict | None = None) -> MagicMock:
    rec = MagicMock()
    rec.id = AGENT_ID
    rec.type = "conversation"
    rec.name = "Conversation"
    rec.system_prompt = None
    rec.config_json = config_json or {}
    rec.autonomy_level = 2
    return rec


def _make_conv_agent(*, config_json: dict | None = None) -> "ConversationAgent":
    from app.agents.conversation import ConversationAgent
    session = MagicMock()
    session.add = MagicMock()
    return ConversationAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(config_json=config_json),
        anthropic_client=MagicMock(),
        db_session=session,
    )


def test_tools_for_telegram_does_not_include_handoff():
    agent = _make_conv_agent()
    tool_names = [t["name"] for t in agent._tools_for_channel("telegram")]
    assert "suggest_telegram_handoff" not in tool_names


def test_tools_for_instagram_includes_handoff():
    agent = _make_conv_agent()
    tool_names = [t["name"] for t in agent._tools_for_channel("instagram")]
    assert "suggest_telegram_handoff" in tool_names


def test_tools_for_instagram_comment_includes_handoff():
    agent = _make_conv_agent()
    tool_names = [t["name"] for t in agent._tools_for_channel("instagram_comment")]
    assert "suggest_telegram_handoff" in tool_names


def test_instagram_system_prompt_contains_concise_rule():
    agent = _make_conv_agent()
    prompt = agent._channel_system_prompt("instagram")
    assert "concise" in prompt.lower()


def test_instagram_system_prompt_contains_plain_text_rule():
    agent = _make_conv_agent()
    prompt = agent._channel_system_prompt("instagram")
    assert "Plain text" in prompt or "plain text" in prompt.lower()


def test_instagram_system_prompt_mentions_handoff_tool():
    agent = _make_conv_agent()
    prompt = agent._channel_system_prompt("instagram")
    assert "suggest_telegram_handoff" in prompt


def test_instagram_system_prompt_mentions_no_phone_policy():
    agent = _make_conv_agent()
    prompt = agent._channel_system_prompt("instagram")
    assert "phone" in prompt.lower()


def test_telegram_system_prompt_has_no_instagram_section():
    agent = _make_conv_agent()
    prompt = agent._channel_system_prompt("telegram")
    assert "suggest_telegram_handoff" not in prompt
    assert "Plain text only" not in prompt


async def test_handoff_tool_with_bot_username():
    agent = _make_conv_agent(config_json={"telegram_bot_username": "@TestBot"})
    result = await agent._tool_suggest_telegram_handoff({"reason": "bulk order"})
    assert "t.me/TestBot" in result["message"]
    assert result["reason"] == "bulk order"


async def test_handoff_tool_without_bot_username():
    agent = _make_conv_agent()
    result = await agent._tool_suggest_telegram_handoff({"reason": "price negotiation"})
    assert "Telegram" in result["message"]
    assert result["reason"] == "price negotiation"


# ═══════════════════════════════════════════════════════════════════════════════
# C. Same agent logic, different channel output
# ═══════════════════════════════════════════════════════════════════════════════

def _make_llm_response(text: str) -> MagicMock:
    """Build a mock Anthropic response that returns ``text`` and stops."""
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    resp.stop_reason = "end_turn"
    resp.usage = MagicMock(input_tokens=50, output_tokens=20)
    client = AsyncMock()
    client.chat = AsyncMock(return_value=resp)
    return client


MARKDOWN_REPLY = (
    "Here are our **best products**:\n"
    "- *Ko'rpa* (quilt): 150,000 UZS\n"
    "- *Yostiq* (pillow): 50,000 UZS\n\n"
    "Would you like to place an order?"
)


@pytest.mark.anyio
async def test_telegram_output_preserves_markdown():
    from app.agents.base import AgentInput
    from app.agents.channel_adapters import get_adapter
    from app.agents.conversation import ConversationAgent

    session = MagicMock()
    session.add = MagicMock()
    agent = ConversationAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(),
        anthropic_client=_make_llm_response(MARKDOWN_REPLY),
        db_session=session,
    )

    output = await agent.handle(
        AgentInput(type="message", payload="show me products", channel="telegram")
    )

    adapter = get_adapter("telegram")
    formatted = adapter.format_message(output.response_text)

    assert "**best products**" in formatted
    assert "*Ko'rpa*" in formatted


@pytest.mark.anyio
async def test_instagram_output_strips_markdown():
    from app.agents.base import AgentInput
    from app.agents.channel_adapters import get_adapter
    from app.agents.conversation import ConversationAgent

    session = MagicMock()
    session.add = MagicMock()
    agent = ConversationAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(),
        anthropic_client=_make_llm_response(MARKDOWN_REPLY),
        db_session=session,
    )

    output = await agent.handle(
        AgentInput(type="message", payload="show me products", channel="instagram")
    )

    adapter = get_adapter("instagram")
    formatted = adapter.format_message(output.response_text)

    assert "**" not in formatted
    assert "*Ko'rpa*" not in formatted
    assert "Ko'rpa" in formatted           # content preserved, marks removed
    assert len(formatted) <= 1000


@pytest.mark.anyio
async def test_same_input_produces_different_formatting():
    """Verify the adapter — not the agent — is responsible for the difference."""
    from app.agents.base import AgentInput
    from app.agents.channel_adapters import get_adapter
    from app.agents.conversation import ConversationAgent

    session = MagicMock()
    session.add = MagicMock()

    tg_agent = ConversationAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(),
        anthropic_client=_make_llm_response(MARKDOWN_REPLY),
        db_session=session,
    )
    ig_agent = ConversationAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(),
        anthropic_client=_make_llm_response(MARKDOWN_REPLY),
        db_session=session,
    )

    tg_output = await tg_agent.handle(
        AgentInput(type="message", payload="show me products", channel="telegram")
    )
    ig_output = await ig_agent.handle(
        AgentInput(type="message", payload="show me products", channel="instagram")
    )

    # Both agents received the same LLM text — raw output should be identical.
    assert tg_output.response_text == ig_output.response_text

    # After adapter formatting, results must differ.
    tg_formatted = get_adapter("telegram").format_message(tg_output.response_text)
    ig_formatted = get_adapter("instagram").format_message(ig_output.response_text)

    assert tg_formatted != ig_formatted
    assert "**" in tg_formatted      # markdown preserved on Telegram
    assert "**" not in ig_formatted  # stripped on Instagram
