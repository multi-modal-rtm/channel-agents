"""ContentAgent — generates Instagram/Telegram marketing content."""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.base import AgentInput, AgentOutput, BaseAgent
from app.integrations.anthropic_client import MODEL_SONNET


class ContentAgent(BaseAgent):
    """
    Content generation specialist.

    Uses claude-sonnet-4-6 for creative quality at reasonable cost.
    Generates post captions, product descriptions, and campaign copy.
    Real prompt implementation is pending (stub for now).
    """

    DEFAULT_MODEL = MODEL_SONNET

    def _default_system_prompt(self) -> str:
        return self._render_template(
            "content_system.j2",
            current_date=datetime.now(UTC).date().isoformat(),
        )

    async def handle(self, input: AgentInput) -> AgentOutput:
        await self._log_action(
            "content.handle",
            {"channel": input.channel, "input_type": input.type},
        )

        payload_text = input.payload if isinstance(input.payload, str) else str(input.payload)
        escalate = self._check_human_approval_needed(payload_text)

        # Stub: real LLM call will replace this in the next iteration.
        return AgentOutput(
            response_text="[Content stub] Here is your generated content.",
            actions_taken=["content.draft_created"],
            confidence=0.85,
            escalate_to_human=escalate,
        )
