"""
Unit tests for OrchestratorAgent: routing logic, hub-and-spoke enforcement,
and specialist call limits.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.base import AgentInput, AgentOutput
from app.agents.conversation import ConversationAgent
from app.agents.content import ContentAgent
from app.agents.lead_qualifier import LeadQualifierAgent
from app.agents.orchestrator import MAX_SPECIALIST_CALLS, OrchestratorAgent
from app.db.models.audit_log import AuditLog

TENANT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
AGENT_ID  = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


# ── Shared builders ───────────────────────────────────────────────────────────

def _make_agent_record(*, autonomy_level: int = 2) -> MagicMock:
    rec = MagicMock()
    rec.id             = AGENT_ID
    rec.type           = "orchestrator"
    rec.name           = "Test Orchestrator"
    rec.system_prompt  = None
    rec.config_json    = {}
    rec.autonomy_level = autonomy_level
    return rec


def _make_routing_response(specialist: str, task: str) -> MagicMock:
    """Build a mock anthropic.types.Message that contains one tool_use routing block."""
    block = MagicMock()
    block.type  = "tool_use"
    block.name  = "route_to_specialist"
    block.input = {"specialist": specialist, "task": task}

    resp = MagicMock()
    resp.content = [block]
    resp.usage   = MagicMock(input_tokens=100, output_tokens=20)
    return resp


def _make_multi_routing_response(decisions: list[tuple[str, str]]) -> MagicMock:
    """Build a response with multiple tool_use blocks."""
    blocks = []
    for specialist, task in decisions:
        b = MagicMock()
        b.type  = "tool_use"
        b.name  = "route_to_specialist"
        b.input = {"specialist": specialist, "task": task}
        blocks.append(b)
    resp = MagicMock()
    resp.content = blocks
    resp.usage   = MagicMock(input_tokens=200, output_tokens=40)
    return resp


def _make_mock_session() -> MagicMock:
    s = MagicMock()
    s.add = MagicMock()
    return s


def _make_orchestrator(
    *,
    routing_response: MagicMock,
    specialists: dict,
    autonomy_level: int = 2,
) -> OrchestratorAgent:
    mock_anthropic = AsyncMock()
    mock_anthropic.chat = AsyncMock(return_value=routing_response)

    return OrchestratorAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(autonomy_level=autonomy_level),
        anthropic_client=mock_anthropic,
        db_session=_make_mock_session(),
        specialists=specialists,
    )


def _stub_specialist(*, response_text: str = "stub", confidence: float = 0.9) -> AsyncMock:
    spec = AsyncMock()
    spec.handle = AsyncMock(
        return_value=AgentOutput(response_text=response_text, confidence=confidence)
    )
    return spec


# ── Routing tests ─────────────────────────────────────────────────────────────

async def test_routes_price_inquiry_to_conversation():
    conv    = _stub_specialist(response_text="Price is $50")
    content = _stub_specialist()
    lead_q  = _stub_specialist()

    orch = _make_orchestrator(
        routing_response=_make_routing_response("conversation", "Customer asks about price"),
        specialists={"conversation": conv, "content": content, "lead_qualifier": lead_q},
    )

    result = await orch.handle(
        AgentInput(type="message", payload="What is the price of ko'rpa?", channel="instagram")
    )

    conv.handle.assert_called_once()
    content.handle.assert_not_called()
    lead_q.handle.assert_not_called()
    assert result.response_text == "Price is $50"


async def test_routes_post_calendar_to_content():
    conv    = _stub_specialist()
    content = _stub_specialist(response_text="Here is your weekly calendar")
    lead_q  = _stub_specialist()

    orch = _make_orchestrator(
        routing_response=_make_routing_response("content", "Generate weekly post calendar"),
        specialists={"conversation": conv, "content": content, "lead_qualifier": lead_q},
    )

    result = await orch.handle(
        AgentInput(type="task", payload="generate weekly post calendar", channel="internal")
    )

    content.handle.assert_called_once()
    conv.handle.assert_not_called()
    assert result.response_text == "Here is your weekly calendar"


async def test_routes_new_contact_to_lead_qualifier():
    conv    = _stub_specialist()
    content = _stub_specialist()
    lead_q  = _stub_specialist(response_text=None, confidence=0.7)

    orch = _make_orchestrator(
        routing_response=_make_routing_response("lead_qualifier", "Classify this new contact"),
        specialists={"conversation": conv, "content": content, "lead_qualifier": lead_q},
    )

    await orch.handle(
        AgentInput(type="message", payload="salom", channel="telegram")
    )

    lead_q.handle.assert_called_once()
    conv.handle.assert_not_called()
    content.handle.assert_not_called()


# ── Hub-and-spoke: specialists never call each other ─────────────────────────

async def test_specialists_receive_only_last_10_messages():
    conv = _stub_specialist()
    long_history = [{"role": "user", "content": f"msg {i}"} for i in range(20)]

    orch = _make_orchestrator(
        routing_response=_make_routing_response("conversation", "Price inquiry"),
        specialists={"conversation": conv, "content": _stub_specialist(), "lead_qualifier": _stub_specialist()},
    )

    await orch.handle(
        AgentInput(
            type="message",
            payload="What is the price?",
            channel="instagram",
            context_history=long_history,
        )
    )

    called_input: AgentInput = conv.handle.call_args[0][0]
    assert len(called_input.context_history) == 10
    assert called_input.context_history == long_history[-10:]


async def test_specialist_receives_task_from_routing_decision():
    conv = _stub_specialist()

    orch = _make_orchestrator(
        routing_response=_make_routing_response("conversation", "Exact task from LLM"),
        specialists={"conversation": conv, "content": _stub_specialist(), "lead_qualifier": _stub_specialist()},
    )

    await orch.handle(
        AgentInput(type="message", payload="original payload", channel="instagram")
    )

    called_input: AgentInput = conv.handle.call_args[0][0]
    assert called_input.payload == "Exact task from LLM"


# ── Hard limit: max 5 specialist calls ───────────────────────────────────────

async def test_hard_limit_max_5_specialist_calls():
    # LLM returns 7 routing decisions — only 5 should be dispatched.
    decisions = [("conversation", f"task {i}") for i in range(7)]
    conv = _stub_specialist()

    mock_anthropic = AsyncMock()
    mock_anthropic.chat = AsyncMock(return_value=_make_multi_routing_response(decisions))

    orch = OrchestratorAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(),
        anthropic_client=mock_anthropic,
        db_session=_make_mock_session(),
        specialists={"conversation": conv, "content": _stub_specialist(), "lead_qualifier": _stub_specialist()},
    )

    await orch.handle(AgentInput(type="task", payload="bulk task", channel="internal"))

    assert conv.handle.call_count == MAX_SPECIALIST_CALLS  # capped at 5, not 7


# ── Empty routing → escalate ─────────────────────────────────────────────────

async def test_no_routing_decision_escalates_to_human():
    empty_resp = MagicMock()
    empty_resp.content = []
    empty_resp.usage   = MagicMock(input_tokens=50, output_tokens=5)

    mock_anthropic = AsyncMock()
    mock_anthropic.chat = AsyncMock(return_value=empty_resp)

    orch = OrchestratorAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(),
        anthropic_client=mock_anthropic,
        db_session=_make_mock_session(),
        specialists={},
    )

    result = await orch.handle(
        AgentInput(type="message", payload="??", channel="instagram")
    )

    assert result.escalate_to_human is True
    assert result.confidence == 0.0


# ── Result aggregation ────────────────────────────────────────────────────────

async def test_aggregate_single_result_passes_through():
    conv = _stub_specialist(response_text="Hello!", confidence=0.95)

    orch = _make_orchestrator(
        routing_response=_make_routing_response("conversation", "task"),
        specialists={"conversation": conv, "content": _stub_specialist(), "lead_qualifier": _stub_specialist()},
    )

    result = await orch.handle(AgentInput(type="message", payload="hi", channel="instagram"))
    assert result.response_text == "Hello!"
    assert result.confidence == pytest.approx(0.95)


async def test_aggregate_multiple_results_uses_min_confidence():
    decisions = [("conversation", "t1"), ("content", "t2")]
    conv    = _stub_specialist(response_text="Conv reply",    confidence=0.9)
    content = _stub_specialist(response_text="Content reply", confidence=0.6)

    mock_anthropic = AsyncMock()
    mock_anthropic.chat = AsyncMock(return_value=_make_multi_routing_response(decisions))

    orch = OrchestratorAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(),
        anthropic_client=mock_anthropic,
        db_session=_make_mock_session(),
        specialists={"conversation": conv, "content": content, "lead_qualifier": _stub_specialist()},
    )

    result = await orch.handle(AgentInput(type="task", payload="combo task", channel="internal"))
    assert result.confidence == pytest.approx(0.6)


async def test_aggregate_any_escalation_propagates():
    decisions = [("conversation", "t1"), ("content", "t2")]
    conv    = _stub_specialist(confidence=0.9)
    content = AsyncMock()
    content.handle = AsyncMock(
        return_value=AgentOutput(escalate_to_human=True, confidence=0.4)
    )

    mock_anthropic = AsyncMock()
    mock_anthropic.chat = AsyncMock(return_value=_make_multi_routing_response(decisions))

    orch = OrchestratorAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(),
        anthropic_client=mock_anthropic,
        db_session=_make_mock_session(),
        specialists={"conversation": conv, "content": content, "lead_qualifier": _stub_specialist()},
    )

    result = await orch.handle(AgentInput(type="task", payload="task", channel="internal"))
    assert result.escalate_to_human is True


# ── Orchestrator logs to audit_log ────────────────────────────────────────────

async def test_orchestrator_handle_writes_audit_log():
    session = _make_mock_session()
    mock_anthropic = AsyncMock()
    mock_anthropic.chat = AsyncMock(
        return_value=_make_routing_response("conversation", "task")
    )

    orch = OrchestratorAgent(
        tenant_id=TENANT_ID,
        agent_db_record=_make_agent_record(),
        anthropic_client=mock_anthropic,
        db_session=session,
        specialists={"conversation": _stub_specialist(), "content": _stub_specialist(), "lead_qualifier": _stub_specialist()},
    )

    await orch.handle(AgentInput(type="message", payload="hi", channel="instagram"))

    assert session.add.called
    row = session.add.call_args[0][0]
    assert isinstance(row, AuditLog)
    assert row.tenant_id == TENANT_ID
    assert row.action == "orchestrator.handle"
