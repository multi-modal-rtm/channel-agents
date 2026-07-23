"""
OrchestratorAgent — the hub in the hub-and-spoke agent architecture.

Responsibilities:
- Receive any incoming event (DM, scheduled task, webhook payload).
- Use the LLM (claude-opus-4-7) to decide which specialist(s) to invoke.
- Enforce the hard limit of MAX_SPECIALIST_CALLS per orchestration.
- Specialists are injected at construction time; they never call each other.
- Pass only the last-10-message slice of history to each specialist.
- Aggregate specialist outputs into a single AgentOutput.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from app.agents.base import AgentInput, AgentOutput, BaseAgent
from app.integrations.anthropic_client import MODEL_OPUS

MAX_SPECIALIST_CALLS = 5

# Tool that the LLM uses to declare which specialist to invoke.
_ROUTING_TOOL: dict[str, Any] = {
    "name": "route_to_specialist",
    "description": (
        "Route the current request to the most appropriate specialist agent. "
        "Call this tool once per specialist you want to invoke. "
        "Do not invoke more than 5 specialists per request."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "specialist": {
                "type": "string",
                "enum": ["conversation", "content", "lead_qualifier", "comment_responder"],
                "description": (
                    "conversation — customer DMs, price/order questions; "
                    "content — post generation, captions, campaign copy; "
                    "lead_qualifier — intent scoring, language detection; "
                    "comment_responder — Instagram comment moderation and replies."
                ),
            },
            "task": {
                "type": "string",
                "description": "Specific task description for the specialist.",
            },
        },
        "required": ["specialist", "task"],
    },
}


class RoutingDecision(BaseModel):
    specialist: Literal["conversation", "content", "lead_qualifier", "comment_responder"]
    task: str


class OrchestratorAgent(BaseAgent):
    """
    Hub agent that routes to specialists.

    ``specialists`` must be supplied by the caller — the orchestrator has no
    knowledge of how to construct them (avoids circular imports and lets tests
    inject mocks cleanly).
    """

    DEFAULT_MODEL = MODEL_OPUS

    def __init__(
        self,
        *,
        specialists: dict[str, BaseAgent] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._specialists: dict[str, BaseAgent] = specialists or {}

    @property
    def tools(self) -> list[dict[str, Any]]:
        return [_ROUTING_TOOL]

    def _default_system_prompt(self) -> str:
        from datetime import UTC, datetime
        return self._render_template(
            "orchestrator_system.j2",
            current_date=datetime.now(UTC).date().isoformat(),
        )

    # ── Entry point ───────────────────────────────────────────────────────────

    async def handle(self, input: AgentInput) -> AgentOutput:
        await self._log_action(
            "orchestrator.handle",
            {"input_type": input.type, "channel": input.channel},
        )

        routing = await self._route(input)
        if not routing:
            self.logger.warning(
                "orchestrator_no_routing_decision",
                payload_preview=str(input.payload)[:100],
            )
            return AgentOutput(escalate_to_human=True, confidence=0.0)

        results: list[AgentOutput] = []
        for i, decision in enumerate(routing):
            if i >= MAX_SPECIALIST_CALLS:
                self.logger.warning(
                    "max_specialist_calls_reached",
                    limit=MAX_SPECIALIST_CALLS,
                    requested=len(routing),
                )
                break

            specialist = self._specialists.get(decision.specialist)
            if specialist is None:
                self.logger.error("unknown_specialist", specialist=decision.specialist)
                continue

            specialist_input = AgentInput(
                type=input.type,
                payload=decision.task,
                # Skill rule: pass only the last 10 messages to each specialist
                context_history=input.context_history[-10:],
                channel=input.channel,
                customer_id=input.customer_id,
            )
            result = await specialist.handle(specialist_input)
            results.append(result)

        return self._aggregate(results)

    # ── Routing ───────────────────────────────────────────────────────────────

    async def _route(self, input: AgentInput) -> list[RoutingDecision]:
        """Ask the LLM which specialist(s) to invoke via Anthropic tool use."""
        payload_text = (
            input.payload
            if isinstance(input.payload, str)
            else str(input.payload)
        )
        response = await self.anthropic_client.chat(
            messages=[{"role": "user", "content": payload_text}],
            model=self.model,
            system=self.system_prompt,
            max_tokens=512,
            tools=self.tools,
            agent_id=self.agent_db_record.id,
        )

        decisions: list[RoutingDecision] = []
        for block in response.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "route_to_specialist":
                try:
                    decisions.append(RoutingDecision(**block.input))
                except Exception:
                    self.logger.warning("invalid_routing_tool_input", raw=str(block.input))

        return decisions

    # ── Aggregation ───────────────────────────────────────────────────────────

    def _aggregate(self, results: list[AgentOutput]) -> AgentOutput:
        if not results:
            return AgentOutput(escalate_to_human=True, confidence=0.0)
        if len(results) == 1:
            return results[0]
        return AgentOutput(
            response_text=results[-1].response_text,
            actions_taken=[a for r in results for a in r.actions_taken],
            suggested_actions=[a for r in results for a in r.suggested_actions],
            confidence=min(r.confidence for r in results),
            escalate_to_human=any(r.escalate_to_human for r in results),
            cost_usd=sum(r.cost_usd for r in results),
        )
