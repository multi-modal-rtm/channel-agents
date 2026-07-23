"""
Unit tests for BaseAgent helpers: audit logging and autonomy level logic.
No DB, no network — all dependencies are mocked.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

# ── Fake Anthropic response (for ConversationAgent which now calls LLM) ───────

def _make_conv_anthropic() -> AsyncMock:
    """Return an AsyncMock anthropic_client whose chat() returns a text end_turn."""
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "Test reply"
    resp = MagicMock()
    resp.content = [text_block]
    resp.stop_reason = "end_turn"
    resp.usage = MagicMock(input_tokens=10, output_tokens=5)
    client = AsyncMock()
    client.chat = AsyncMock(return_value=resp)
    return client

import pytest

from app.agents.base import AgentInput, AgentOutput, BaseAgent
from app.agents.conversation import ConversationAgent
from app.db.models.audit_log import AuditLog

TENANT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
AGENT_ID  = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_agent_record(
    *,
    autonomy_level: int = 2,
    system_prompt: str | None = None,
    config_json: dict | None = None,
    agent_type: str = "conversation",
) -> MagicMock:
    rec = MagicMock()
    rec.id            = AGENT_ID
    rec.type          = agent_type
    rec.name          = f"Test {agent_type}"
    rec.system_prompt = system_prompt
    rec.config_json   = config_json or {}
    rec.autonomy_level = autonomy_level
    return rec


def _make_conversation_agent(*, autonomy_level: int = 2) -> ConversationAgent:
    session = MagicMock()
    session.add = MagicMock()
    return ConversationAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(autonomy_level=autonomy_level),
        anthropic_client=MagicMock(),
        db_session=session,
    )


# ── Autonomy level: _check_human_approval_needed ──────────────────────────────

def test_level_0_always_escalates_harmless():
    agent = _make_conversation_agent(autonomy_level=0)
    assert agent._check_human_approval_needed("just say hello") is True


def test_level_0_always_escalates_risky():
    agent = _make_conversation_agent(autonomy_level=0)
    assert agent._check_human_approval_needed("process refund payment") is True


def test_level_2_never_escalates_harmless():
    agent = _make_conversation_agent(autonomy_level=2)
    assert agent._check_human_approval_needed("say hello to the customer") is False


def test_level_2_never_escalates_risky():
    agent = _make_conversation_agent(autonomy_level=2)
    assert agent._check_human_approval_needed("send payment confirmation to customer") is False


def test_level_1_escalates_on_payment_keyword():
    agent = _make_conversation_agent(autonomy_level=1)
    assert agent._check_human_approval_needed("process customer payment") is True


def test_level_1_escalates_on_refund():
    agent = _make_conversation_agent(autonomy_level=1)
    assert agent._check_human_approval_needed("issue refund for damaged goods") is True


def test_level_1_does_not_escalate_harmless():
    agent = _make_conversation_agent(autonomy_level=1)
    assert agent._check_human_approval_needed("reply with product catalog link") is False


def test_level_1_escalates_on_complaint():
    agent = _make_conversation_agent(autonomy_level=1)
    assert agent._check_human_approval_needed("customer filed a complaint") is True


# ── model property ─────────────────────────────────────────────────────────────

def test_model_uses_config_json_override():
    agent = _make_conversation_agent()
    agent.agent_db_record.config_json = {"model": "claude-opus-4-7"}
    assert agent.model == "claude-opus-4-7"


def test_model_falls_back_to_default():
    agent = _make_conversation_agent()
    agent.agent_db_record.config_json = {}
    from app.integrations.anthropic_client import MODEL_SONNET
    assert agent.model == MODEL_SONNET


# ── system_prompt property ─────────────────────────────────────────────────────

def test_system_prompt_uses_db_override():
    agent = _make_conversation_agent()
    agent.agent_db_record.system_prompt = "Custom system prompt override."
    assert agent.system_prompt == "Custom system prompt override."


def test_system_prompt_falls_back_to_template():
    agent = _make_conversation_agent()
    agent.agent_db_record.system_prompt = None
    # Template renders without error and contains expected content
    prompt = agent.system_prompt
    assert "current_date" not in prompt   # variable should be substituted
    assert len(prompt) > 10


# ── audit logging ─────────────────────────────────────────────────────────────

async def test_log_action_adds_audit_log_row_with_correct_tenant():
    session = MagicMock()
    session.add = MagicMock()
    agent = ConversationAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(),
        anthropic_client=MagicMock(),
        db_session=session,
    )

    await agent._log_action("test.action", {"key": "value"})

    assert session.add.called
    row = session.add.call_args[0][0]
    assert isinstance(row, AuditLog)
    assert row.tenant_id == TENANT_ID
    assert row.agent_id  == AGENT_ID
    assert row.action    == "test.action"
    assert row.payload_json == {"key": "value"}


async def test_log_action_uses_agent_id_as_default_entity():
    session = MagicMock()
    session.add = MagicMock()
    agent = ConversationAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(),
        anthropic_client=MagicMock(),
        db_session=session,
    )

    await agent._log_action("entity.default")

    row = session.add.call_args[0][0]
    assert row.entity_id == AGENT_ID


async def test_log_action_accepts_custom_entity():
    session = MagicMock()
    session.add = MagicMock()
    custom_id = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    agent = ConversationAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(),
        anthropic_client=MagicMock(),
        db_session=session,
    )

    await agent._log_action("msg.sent", entity_type="conversation", entity_id=custom_id)

    row = session.add.call_args[0][0]
    assert row.entity_type == "conversation"
    assert row.entity_id   == custom_id


# ── specialist handle() logs audit entry ──────────────────────────────────────

async def test_conversation_handle_logs_to_audit_log():
    session = MagicMock()
    session.add = MagicMock()
    agent = ConversationAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(autonomy_level=2),
        anthropic_client=_make_conv_anthropic(),
        db_session=session,
    )

    result = await agent.handle(
        AgentInput(type="message", payload="What is the price?", channel="instagram")
    )

    assert session.add.called
    row = session.add.call_args[0][0]
    assert isinstance(row, AuditLog)
    assert row.tenant_id == TENANT_ID
    assert row.action == "conversation.handle"


async def test_content_handle_logs_to_audit_log():
    from app.agents.content import ContentAgent

    session = MagicMock()
    session.add = MagicMock()
    agent = ContentAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(agent_type="content", autonomy_level=2),
        anthropic_client=MagicMock(),
        db_session=session,
    )

    await agent.handle(AgentInput(type="task", payload="generate post", channel="internal"))

    row = session.add.call_args[0][0]
    assert row.tenant_id == TENANT_ID
    assert row.action == "content.handle"


# ── _load_knowledge returns empty when no qdrant client ───────────────────────

async def test_load_knowledge_without_qdrant_returns_empty():
    agent = _make_conversation_agent()
    result = await agent._load_knowledge("quilts price", top_k=5)
    assert result == []
