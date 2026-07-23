"""LeadQualifierAgent — fast intent classification and lead scoring."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.base import AgentInput, AgentOutput, BaseAgent
from app.integrations.anthropic_client import MODEL_HAIKU


class LeadQualifierAgent(BaseAgent):
    """
    Lead classification specialist.

    Uses claude-haiku-4-5 for high-volume, low-cost classification tasks:
    intent scoring, language detection, urgency tagging.
    Real prompt implementation is pending (stub for now).
    """

    DEFAULT_MODEL = MODEL_HAIKU

    def _default_system_prompt(self) -> str:
        return self._render_template(
            "lead_qualifier_system.j2",
            current_date=datetime.now(UTC).date().isoformat(),
        )

    async def handle(self, input: AgentInput) -> AgentOutput:
        await self._log_action(
            "lead_qualifier.handle",
            {"channel": input.channel, "input_type": input.type},
        )

        # Stub: real LLM classification call will replace this.
        return AgentOutput(
            response_text=None,
            actions_taken=["lead.classified"],
            suggested_actions=["route_to_conversation"],
            confidence=0.7,
            escalate_to_human=False,
        )
