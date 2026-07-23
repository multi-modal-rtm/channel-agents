"""
Base agent infrastructure: data models + abstract class all agents inherit from.

Design rules (from agent-design SKILL):
- Hub-and-spoke: orchestrator calls specialists, never the reverse.
- ContextVar tenant_id must be set before any DB or LLM call.
- Every handle() call must produce an audit_log entry (Rule 7).
- Every LLM call goes through TenantAwareAnthropicClient (Rule 6).
- Autonomy levels: 0=draft-only, 1=propose, 2=autonomous.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import structlog
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.agent import Agent
from app.db.models.audit_log import AuditLog
from app.integrations.anthropic_client import MODEL_SONNET, TenantAwareAnthropicClient

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_jinja_env = Environment(
    loader=FileSystemLoader(_PROMPTS_DIR),
    trim_blocks=True,
    lstrip_blocks=True,
)

# Keywords that trigger level-1 escalation
_ESCALATION_KEYWORDS = frozenset({
    "send", "payment", "order", "purchase", "refund",
    "money", "cancel", "complaint", "legal",
})


# ── Data models ───────────────────────────────────────────────────────────────

class Doc(BaseModel):
    """A document returned by the knowledge-base RAG lookup."""
    id: str
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentInput(BaseModel):
    type: Literal["message", "task", "event"]
    payload: str | dict[str, Any]
    context_history: list[dict[str, Any]] = Field(default_factory=list)
    channel: str                          # instagram | telegram | internal
    customer_id: str | None = None


class AgentOutput(BaseModel):
    response_text: str | None = None
    actions_taken: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    escalate_to_human: bool = False
    cost_usd: float = 0.0
    # Instagram-specific structured responses (set by show_product_carousel /
    # suggest_quick_replies tools; ignored for non-Instagram channels)
    quick_reply_options: list[str] = Field(default_factory=list)
    carousel_products: list[dict] = Field(default_factory=list)


# ── Base class ────────────────────────────────────────────────────────────────

class BaseAgent(ABC):
    """Abstract base for all agents.

    Subclasses must implement:
      - DEFAULT_MODEL  (class attribute)
      - handle(input)  (async method)
      - _default_system_prompt()  (method; used when agent_db_record.system_prompt is None)
    """

    DEFAULT_MODEL: str = MODEL_SONNET

    def __init__(
        self,
        *,
        tenant_id: UUID,
        agent_db_record: Agent,
        anthropic_client: TenantAwareAnthropicClient,
        db_session: AsyncSession,
        qdrant_client: Any | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.agent_db_record = agent_db_record
        self.anthropic_client = anthropic_client
        self.db_session = db_session
        self._qdrant_client = qdrant_client
        self.logger = structlog.get_logger(type(self).__module__).bind(
            agent=type(self).__name__,
            tenant_id=str(tenant_id),
            agent_id=str(agent_db_record.id),
        )

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def model(self) -> str:
        """Active model: from config_json override, else subclass default."""
        return self.agent_db_record.config_json.get("model", self.DEFAULT_MODEL)

    @property
    def autonomy_level(self) -> int:
        return self.agent_db_record.autonomy_level

    @property
    def tools(self) -> list[dict[str, Any]]:
        """Anthropic tool definitions for this agent. Override per subclass."""
        return []

    @property
    def system_prompt(self) -> str:
        """DB-level override takes precedence; fall back to rendered template."""
        override = self.agent_db_record.system_prompt
        return override if override else self._default_system_prompt()

    # ── Abstract ──────────────────────────────────────────────────────────────

    @abstractmethod
    async def handle(self, input: AgentInput) -> AgentOutput:
        """Process an input and return an output. Must call _log_action."""
        ...

    def _default_system_prompt(self) -> str:
        raise NotImplementedError(
            f"{type(self).__name__} must implement _default_system_prompt()"
        )

    # ── Common helpers ────────────────────────────────────────────────────────

    def _check_human_approval_needed(self, decision: str) -> bool:
        """True when this decision requires a human to approve before acting.

        level 0 → always gate (safe default for new agents — first 7 days)
        level 1 → gate only when decision involves money or external messages
        level 2 → fully autonomous, never gates
        """
        if self.autonomy_level == 0:
            return True
        if self.autonomy_level >= 2:
            return False
        # Level 1: escalate on high-risk keywords
        text = decision.lower()
        return any(kw in text for kw in _ESCALATION_KEYWORDS)

    async def _load_knowledge(self, query: str, top_k: int = 5) -> list[Doc]:
        """Semantic search over the tenant's knowledge base in Qdrant.

        Returns an empty list when no Qdrant client is injected (tests / agents
        that don't use RAG).  Embedding step is deferred until an embedding
        provider is wired up.
        """
        if self._qdrant_client is None:
            return []
        # TODO: embed query via an embedding model, then search:
        #   collection = f"{settings.qdrant_collection_prefix}{self.tenant_id}"
        #   results = self._qdrant_client.search(collection, query_vector=…, limit=top_k)
        return []

    async def _log_action(
        self,
        action: str,
        payload: dict[str, Any] | None = None,
        *,
        entity_type: str = "agent",
        entity_id: UUID | None = None,
    ) -> None:
        """Append an audit log entry to the current session (Rule 7).

        Uses session.add() — the caller's transaction commit writes the row.
        Exceptions are caught and logged so they never mask the agent's result.
        """
        try:
            self.db_session.add(
                AuditLog(
                    tenant_id=self.tenant_id,
                    agent_id=self.agent_db_record.id,
                    action=action,
                    entity_type=entity_type,
                    entity_id=entity_id or self.agent_db_record.id,
                    payload_json=payload or {},
                )
            )
        except Exception as exc:
            self.logger.error("audit_log_add_failed", error=str(exc), action=action)

    def _render_template(self, template_name: str, **ctx: Any) -> str:
        return _jinja_env.get_template(template_name).render(**ctx)
