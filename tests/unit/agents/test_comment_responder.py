"""
Tests for CommentResponderAgent.

Three action paths:
  A. ignore  — spam/blocked-keyword fast-path (no LLM call)
  B. public_reply  — LLM decides short public reply only
  C. public_reply_and_dm  — LLM decides public reply + private DM

Also covers:
  - competitor mention → escalate_to_human=True
  - public_text hard-truncated to 150 chars
  - no tool_call fallback → ignore
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.comment_responder import CommentResponderAgent, _PUBLIC_TEXT_MAX

TENANT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
AGENT_ID  = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


# ── Fixtures / helpers ────────────────────────────────────────────────────────

def _make_agent_record(config_json: dict | None = None) -> MagicMock:
    rec = MagicMock()
    rec.id = AGENT_ID
    rec.type = "comment_responder"
    rec.name = "Comment Responder"
    rec.system_prompt = None
    rec.config_json = config_json or {}
    rec.autonomy_level = 2
    return rec


def _make_agent(config_json: dict | None = None) -> CommentResponderAgent:
    session = MagicMock()
    session.add = MagicMock()
    return CommentResponderAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(config_json=config_json),
        anthropic_client=MagicMock(),
        db_session=session,
    )


def _tool_response(
    action: str,
    public_text: str | None = None,
    dm_text: str | None = None,
    escalate: bool = False,
) -> MagicMock:
    """Build a mock Anthropic response containing a submit_decision tool call."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "submit_decision"
    block.input = {
        "action": action,
        "public_text": public_text,
        "dm_text": dm_text,
        "escalate": escalate,
    }
    resp = MagicMock()
    resp.content = [block]
    resp.stop_reason = "tool_use"
    resp.usage = MagicMock(input_tokens=30, output_tokens=15)

    client = AsyncMock()
    client.chat = AsyncMock(return_value=resp)
    return client


def _no_tool_response() -> MagicMock:
    """Build a mock response with a text block instead of a tool call."""
    block = MagicMock()
    block.type = "text"
    block.text = "I think we should ignore this."
    resp = MagicMock()
    resp.content = [block]
    resp.stop_reason = "end_turn"
    resp.usage = MagicMock(input_tokens=20, output_tokens=10)

    client = AsyncMock()
    client.chat = AsyncMock(return_value=resp)
    return client


# ── A. ignore path ────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_blocked_keyword_returns_ignore_without_llm_call():
    """Blocked keyword fast-path must not call the LLM."""
    from app.agents.base import AgentInput
    agent = _make_agent(config_json={"blocked_keywords": ["spam", "free money"]})
    mock_client = AsyncMock()
    agent.anthropic_client = mock_client

    inp = AgentInput(type="event", payload="Free money click here!", channel="instagram_comment")
    output = await agent.handle(inp)

    decision = json.loads(output.response_text)
    assert decision["action"] == "ignore"
    assert decision["public_text"] is None
    mock_client.chat.assert_not_called()


@pytest.mark.anyio
async def test_blocked_keyword_case_insensitive():
    from app.agents.base import AgentInput
    agent = _make_agent(config_json={"blocked_keywords": ["SPAM"]})
    agent.anthropic_client = AsyncMock()

    inp = AgentInput(type="event", payload="spam is here", channel="instagram_comment")
    output = await agent.handle(inp)

    decision = json.loads(output.response_text)
    assert decision["action"] == "ignore"
    agent.anthropic_client.chat.assert_not_called()


@pytest.mark.anyio
async def test_llm_decides_ignore():
    """LLM returns ignore action — no public_text in output."""
    from app.agents.base import AgentInput
    agent = _make_agent()
    agent.anthropic_client = _tool_response("ignore", public_text=None)

    inp = AgentInput(
        type="event",
        payload={"comment_text": "🤣🤣🤣", "post_caption": "Ko'rpa 30% off"},
        channel="instagram_comment",
    )
    output = await agent.handle(inp)

    decision = json.loads(output.response_text)
    assert decision["action"] == "ignore"
    assert decision["public_text"] is None
    assert "comment.moderation.ignore" in output.actions_taken
    assert output.escalate_to_human is False


# ── B. public_reply path ──────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_public_reply_path():
    from app.agents.base import AgentInput
    agent = _make_agent()
    agent.anthropic_client = _tool_response(
        "public_reply",
        public_text="Rahmat! Ko'proq ma'lumot uchun DM yozing 😊",
    )

    inp = AgentInput(
        type="event",
        payload={"comment_text": "Juda chiroyli ko'rpa!", "post_caption": "Yangi kolleksiya"},
        channel="instagram_comment",
    )
    output = await agent.handle(inp)

    decision = json.loads(output.response_text)
    assert decision["action"] == "public_reply"
    assert decision["public_text"] == "Rahmat! Ko'proq ma'lumot uchun DM yozing 😊"
    assert decision["dm_text"] is None
    assert "comment.moderation.public_reply" in output.actions_taken


@pytest.mark.anyio
async def test_public_text_truncated_to_150_chars():
    """Hard enforcement: public_text > 150 chars must be truncated."""
    from app.agents.base import AgentInput
    agent = _make_agent()
    long_text = "A" * 200
    agent.anthropic_client = _tool_response("public_reply", public_text=long_text)

    inp = AgentInput(
        type="event",
        payload="Great product!",
        channel="instagram_comment",
    )
    output = await agent.handle(inp)

    decision = json.loads(output.response_text)
    assert len(decision["public_text"]) == _PUBLIC_TEXT_MAX


@pytest.mark.anyio
async def test_string_payload_handled():
    """Bare string payload (not a dict) should be treated as comment_text."""
    from app.agents.base import AgentInput
    agent = _make_agent()
    agent.anthropic_client = _tool_response("public_reply", public_text="Yaxshi!")

    inp = AgentInput(
        type="event",
        payload="Narxi qancha?",
        channel="instagram_comment",
    )
    output = await agent.handle(inp)

    decision = json.loads(output.response_text)
    assert decision["action"] == "public_reply"


# ── C. public_reply_and_dm path ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_public_reply_and_dm_path():
    from app.agents.base import AgentInput
    agent = _make_agent()
    agent.anthropic_client = _tool_response(
        "public_reply_and_dm",
        public_text="Ha, bor! DM yozing narx uchun.",
        dm_text=(
            "Salom! Ko'rpa 150x200 narxi — 220 000 so'm. "
            "Buyurtma berish uchun telefon raqamingizni yuboring."
        ),
    )

    inp = AgentInput(
        type="event",
        payload={
            "comment_text": "Bu ko'rpa sotuvdami? Narxi?",
            "post_caption": "Ko'rpa 150x200 — yangi kolleksiya",
        },
        channel="instagram_comment",
    )
    output = await agent.handle(inp)

    decision = json.loads(output.response_text)
    assert decision["action"] == "public_reply_and_dm"
    assert decision["public_text"] == "Ha, bor! DM yozing narx uchun."
    assert "buyurtma" in decision["dm_text"].lower()
    assert "comment.moderation.public_reply_and_dm" in output.actions_taken
    assert output.escalate_to_human is False


@pytest.mark.anyio
async def test_competitor_mention_sets_escalate():
    from app.agents.base import AgentInput
    agent = _make_agent(config_json={"competitor_brands": ["BrandX", "RivalCo"]})
    agent.anthropic_client = _tool_response("ignore", escalate=True)

    inp = AgentInput(
        type="event",
        payload={"comment_text": "BrandX is better!", "post_caption": "Ko'rpa"},
        channel="instagram_comment",
    )
    output = await agent.handle(inp)

    assert output.escalate_to_human is True
    decision = json.loads(output.response_text)
    assert decision["action"] == "ignore"


# ── Fallback: no tool call ────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_no_tool_call_falls_back_to_ignore():
    """If the LLM returns text instead of a tool call, default to ignore."""
    from app.agents.base import AgentInput
    agent = _make_agent()
    agent.anthropic_client = _no_tool_response()

    inp = AgentInput(
        type="event",
        payload="Random comment",
        channel="instagram_comment",
    )
    output = await agent.handle(inp)

    decision = json.loads(output.response_text)
    assert decision["action"] == "ignore"


# ── System prompt content ─────────────────────────────────────────────────────

def test_system_prompt_contains_submit_decision():
    agent = _make_agent()
    prompt = agent._default_system_prompt()
    assert "submit_decision" in prompt


def test_system_prompt_contains_competitor_brands():
    agent = _make_agent(config_json={"competitor_brands": ["AcmeCorp", "MegaBrand"]})
    prompt = agent._default_system_prompt()
    assert "AcmeCorp" in prompt
    assert "MegaBrand" in prompt


def test_system_prompt_with_no_competitor_brands():
    agent = _make_agent()
    prompt = agent._default_system_prompt()
    assert "Competitor brands" not in prompt


def test_system_prompt_mentions_plain_text_rule():
    agent = _make_agent()
    prompt = agent._default_system_prompt()
    assert "Plain text" in prompt or "plain text" in prompt.lower()


def test_system_prompt_mentions_150_char_limit():
    agent = _make_agent()
    prompt = agent._default_system_prompt()
    assert "150" in prompt
