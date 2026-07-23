"""CommentResponderAgent — classify and reply to Instagram comments.

Uses claude-haiku-4-5 for cost-efficient, high-volume moderation.

Decision is produced via a forced tool call (submit_decision) with:
  action:       "ignore" | "public_reply" | "public_reply_and_dm"
  public_text:  str | None   — public comment reply (max 150 chars)
  dm_text:      str | None   — private DM body
  escalate:     bool         — flag for human review

Moderation shortcuts (no LLM call):
  blocked_keywords  (config_json) — auto-ignore matching comments
  competitor_brands (config_json) — LLM prompted to ignore + escalate
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog

from app.agents.base import AgentInput, AgentOutput, BaseAgent
from app.integrations.anthropic_client import MODEL_HAIKU, MODEL_COSTS

logger = structlog.get_logger(__name__)

_PUBLIC_TEXT_MAX = 150

_SUBMIT_DECISION_TOOL: dict = {
    "name": "submit_decision",
    "description": "Submit your moderation decision for this comment.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["ignore", "public_reply", "public_reply_and_dm"],
                "description": "What to do with this comment.",
            },
            "public_text": {
                "type": "string",
                "description": (
                    "Public reply text, max 150 characters, plain text. "
                    "Required for public_reply and public_reply_and_dm. Omit for ignore."
                ),
            },
            "dm_text": {
                "type": "string",
                "description": "Private DM body. Required for public_reply_and_dm only.",
            },
            "escalate": {
                "type": "boolean",
                "description": (
                    "True if a human operator should review this comment "
                    "(competitor mention, complaint, refund request)."
                ),
            },
        },
        "required": ["action", "escalate"],
    },
}


class CommentResponderAgent(BaseAgent):
    DEFAULT_MODEL = MODEL_HAIKU

    def _default_system_prompt(self) -> str:
        cfg = self.agent_db_record.config_json
        return self._render_template(
            "comment_responder_system.j2",
            current_date=datetime.now(UTC).date().isoformat(),
            blocked_keywords=cfg.get("blocked_keywords", []),
            competitor_brands=cfg.get("competitor_brands", []),
        )

    async def handle(self, input: AgentInput) -> AgentOutput:
        await self._log_action(
            "comment_responder.handle",
            {"channel": input.channel, "input_type": input.type},
        )

        comment_text, post_caption = _parse_payload(input.payload)

        # Fast-path: blocked keyword check (no LLM call needed)
        blocked = self.agent_db_record.config_json.get("blocked_keywords", [])
        if _matches_keywords(comment_text, blocked):
            await self._log_action(
                "comment_responder.blocked_keyword",
                {"comment_preview": comment_text[:100]},
            )
            return _make_output("ignore", None, None, escalate=False, cost=0.0)

        system = self.agent_db_record.system_prompt or self._default_system_prompt()
        user_msg = _build_user_message(comment_text, post_caption)

        resp = await self.anthropic_client.chat(
            messages=[{"role": "user", "content": user_msg}],
            model=self.model,
            system=system,
            max_tokens=512,
            tools=[_SUBMIT_DECISION_TOOL],
            agent_id=self.agent_db_record.id,
        )

        decision = _extract_decision(resp)
        if decision is None:
            logger.warning(
                "comment_responder.no_tool_call",
                comment_preview=comment_text[:80],
            )
            decision = {"action": "ignore", "escalate": False}

        action = decision.get("action", "ignore")
        public_text = decision.get("public_text") or None
        dm_text = decision.get("dm_text") or None
        escalate = bool(decision.get("escalate", False))

        # Hard-enforce the character limit even if the LLM ignores it
        if public_text and len(public_text) > _PUBLIC_TEXT_MAX:
            public_text = public_text[:_PUBLIC_TEXT_MAX]

        cost = _estimate_cost(self.model, resp)

        return _make_output(action, public_text, dm_text, escalate=escalate, cost=cost)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_payload(payload: str | dict) -> tuple[str, str]:
    if isinstance(payload, dict):
        return (
            str(payload.get("comment_text", "")),
            str(payload.get("post_caption", "")),
        )
    return str(payload), ""


def _matches_keywords(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords)


def _build_user_message(comment_text: str, post_caption: str) -> str:
    parts = []
    if post_caption:
        parts.append(f"Post caption: {post_caption[:300]}")
    parts.append(f"Comment: {comment_text}")
    return "\n\n".join(parts)


def _extract_decision(resp: Any) -> dict | None:
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_decision":
            return block.input
    return None


def _make_output(
    action: str,
    public_text: str | None,
    dm_text: str | None,
    *,
    escalate: bool,
    cost: float,
) -> AgentOutput:
    return AgentOutput(
        response_text=json.dumps({
            "action": action,
            "public_text": public_text,
            "dm_text": dm_text,
        }),
        actions_taken=[f"comment.moderation.{action}"],
        confidence=0.9,
        escalate_to_human=escalate,
        cost_usd=cost,
    )


def _estimate_cost(model: str, resp: Any) -> float:
    if not hasattr(resp, "usage") or resp.usage is None:
        return 0.0
    for prefix, rates in MODEL_COSTS.items():
        if model.startswith(prefix):
            return (
                resp.usage.input_tokens * rates["input"]
                + resp.usage.output_tokens * rates["output"]
            )
    return 0.0
