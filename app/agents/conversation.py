"""ConversationAgent — handles customer DMs on Instagram and Telegram.

Implements a MAX_TOOL_ROUNDS agentic loop using Anthropic tool use.

Core tools (all channels):
  lookup_product, check_availability, create_order_draft,
  generate_payment_link, escalate_to_human

Instagram-only tool:
  suggest_telegram_handoff — produces a Telegram deep-link CTA when a
  customer needs a richer conversation than Instagram DMs support.

Channel-specific formatting (length, markdown) is handled by the
ChannelAdapter in the background task after this agent returns — the
agent is intentionally channel-agnostic in its LLM logic.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import structlog

from app.agents.base import AgentInput, AgentOutput, BaseAgent
from app.integrations.anthropic_client import MODEL_SONNET

logger = structlog.get_logger(__name__)

MAX_TOOL_ROUNDS = 5

_INSTAGRAM_CHANNELS = frozenset({"instagram", "instagram_comment"})

# ── Tool definitions ──────────────────────────────────────────────────────────

_TOOLS: list[dict] = [
    {
        "name": "lookup_product",
        "description": (
            "Search the product catalog for items matching a name, category, or keyword. "
            "Returns a list of matching products with name, price, and stock status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Product name, type, or keyword"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "check_availability",
        "description": "Check whether a specific product SKU is currently in stock.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "Product SKU code"},
            },
            "required": ["sku"],
        },
    },
    {
        "name": "create_order_draft",
        "description": (
            "Create a draft order for the customer. Call this after collecting their phone number."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_sku": {"type": "string"},
                "quantity": {"type": "integer", "default": 1},
                "customer_phone": {"type": "string"},
                "notes": {"type": "string"},
            },
            "required": ["product_sku", "customer_phone"],
        },
    },
    {
        "name": "generate_payment_link",
        "description": "Generate a payment link for a draft order. Returns a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": (
            "Transfer this conversation to a human operator. "
            "Use when: refund request, complaint, legal question, confidence < 0.7, "
            "or customer explicitly asks for a human."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why escalation is needed"},
            },
            "required": ["reason"],
        },
    },
]

_SUGGEST_QUICK_REPLIES_TOOL: dict = {
    "name": "suggest_quick_replies",
    "description": (
        "Add quick reply buttons below your text message. "
        "Use when the customer's next move is one of 2–4 clear options "
        "(e.g. asking for price, delivery info, sizes, or switching to Telegram). "
        "Buttons appear as tappable chips on Instagram — keep labels under 20 characters."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2–4 short button labels (max 20 chars each)",
                "minItems": 2,
                "maxItems": 4,
            },
        },
        "required": ["options"],
    },
}

_SHOW_PRODUCT_CAROUSEL_TOOL: dict = {
    "name": "show_product_carousel",
    "description": (
        "Display a visual product carousel with cards (image, title, price, Order button). "
        "Use when the customer asks to see multiple products "
        "(e.g. 'qanday ko\'rpalar bor?', 'show me your pillows'). "
        "Pass a search query — the tool looks up matching products automatically."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Product name, type, or keyword to search for",
            },
        },
        "required": ["query"],
    },
}

_SUGGEST_TELEGRAM_HANDOFF_TOOL: dict = {
    "name": "suggest_telegram_handoff",
    "description": (
        "Generate a message inviting the customer to continue on Telegram. "
        "Use when the customer wants to discuss more than 2 products, negotiate bulk pricing, "
        "or the conversation requires more detail than Instagram DMs support. "
        "Do NOT share phone numbers on Instagram — use this tool instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Brief reason for suggesting Telegram (e.g. 'bulk order discussion')",
            },
        },
        "required": ["reason"],
    },
}


class ConversationAgent(BaseAgent):
    DEFAULT_MODEL = MODEL_SONNET

    def _default_system_prompt(self) -> str:
        return self._render_template(
            "conversation_system.j2",
            current_date=datetime.now(UTC).date().isoformat(),
            channel=None,
        )

    def _channel_system_prompt(self, channel: str) -> str:
        """Render the system prompt with channel-specific additions."""
        return self._render_template(
            "conversation_system.j2",
            current_date=datetime.now(UTC).date().isoformat(),
            channel=channel,
        )

    def _tools_for_channel(self, channel: str) -> list[dict]:
        """Return tool list; inject Instagram-only tools for Instagram channels."""
        if channel in _INSTAGRAM_CHANNELS:
            return [
                *_TOOLS,
                _SUGGEST_QUICK_REPLIES_TOOL,
                _SHOW_PRODUCT_CAROUSEL_TOOL,
                _SUGGEST_TELEGRAM_HANDOFF_TOOL,
            ]
        return _TOOLS

    @property
    def tools(self) -> list[dict]:
        return _TOOLS

    async def handle(self, input: AgentInput) -> AgentOutput:
        await self._log_action(
            "conversation.handle",
            {"channel": input.channel, "input_type": input.type},
        )

        payload_text = input.payload if isinstance(input.payload, str) else str(input.payload)
        channel = input.channel

        # Channel-aware system prompt; DB override takes precedence.
        system = (
            self.agent_db_record.system_prompt
            or self._channel_system_prompt(channel)
        )
        tools = self._tools_for_channel(channel)

        history = list(input.context_history)
        history.append({"role": "user", "content": payload_text})

        # Store customer_id for the handoff tool to mint a real token
        self._active_customer_id: str | None = input.customer_id

        escalate = False
        response_text: str | None = None
        cost_usd = 0.0
        round_count = 0
        pending_quick_replies: list[str] = []
        pending_carousel: list[dict] = []

        while round_count < MAX_TOOL_ROUNDS:
            round_count += 1
            resp = await self.anthropic_client.chat(
                messages=history,
                model=self.model,
                system=system,
                max_tokens=1024,
                tools=tools,
                agent_id=self.agent_db_record.id,
            )

            if hasattr(resp, "usage") and resp.usage:
                cost_usd += self._estimate_cost(resp)

            assistant_content = _content_to_dicts(resp.content)
            history.append({"role": "assistant", "content": assistant_content})

            stop_reason = getattr(resp, "stop_reason", None)

            if stop_reason == "end_turn":
                response_text = _extract_text(resp.content)
                break

            if stop_reason != "tool_use":
                response_text = _extract_text(resp.content)
                break

            tool_results = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue

                tool_name = block.name
                tool_input = block.input
                tool_use_id = block.id

                result = await self._dispatch_tool(tool_name, tool_input)

                if tool_name == "escalate_to_human":
                    escalate = True
                elif tool_name == "suggest_quick_replies":
                    pending_quick_replies = result.get("options", [])
                elif tool_name == "show_product_carousel":
                    pending_carousel = result.get("products", [])

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": json.dumps(result),
                })

            if tool_results:
                history.append({"role": "user", "content": tool_results})

            if escalate:
                break

        if not response_text:
            response_text = _extract_text_from_history(history)

        if not escalate:
            escalate = self._check_human_approval_needed(payload_text)

        confidence = 0.5 if escalate else 0.9

        return AgentOutput(
            response_text=response_text,
            confidence=confidence,
            escalate_to_human=escalate,
            cost_usd=cost_usd,
            quick_reply_options=pending_quick_replies,
            carousel_products=pending_carousel,
        )

    # ── Tool dispatch ─────────────────────────────────────────────────────────

    async def _dispatch_tool(self, name: str, input_data: dict) -> dict:
        if name == "lookup_product":
            return await self._tool_lookup_product(input_data.get("query", ""))
        if name == "check_availability":
            return await self._tool_check_availability(input_data.get("sku", ""))
        if name == "create_order_draft":
            return await self._tool_create_order_draft(input_data)
        if name == "generate_payment_link":
            return {"url": f"https://pay.example.com/draft/{input_data.get('order_id', '')}"}
        if name == "escalate_to_human":
            return {"status": "escalated", "reason": input_data.get("reason", "")}
        if name == "suggest_telegram_handoff":
            return await self._tool_suggest_telegram_handoff(input_data)
        if name == "suggest_quick_replies":
            return self._tool_suggest_quick_replies(input_data)
        if name == "show_product_carousel":
            return await self._tool_show_product_carousel(input_data.get("query", ""))
        return {"error": f"unknown tool: {name}"}

    async def _tool_lookup_product(self, query: str) -> dict:
        from app.core.tenant_context import tenant_context
        from app.db.models.product import Product
        from app.db.session import get_tenant_session
        from sqlalchemy import select

        try:
            with tenant_context(self.tenant_id):
                async with get_tenant_session() as session:
                    result = await session.execute(
                        select(Product)
                        .where(Product.tenant_id == self.tenant_id)
                        .where(Product.in_stock == True)
                        .limit(5)
                    )
                    products = result.scalars().all()
                    q = query.lower()
                    matched = [
                        p for p in products
                        if q in (p.name_uz or "").lower()
                        or q in (p.name_ru or "").lower()
                        or q in (p.description or "").lower()
                    ] or list(products)[:3]
                    return {
                        "products": [
                            {
                                "sku": p.sku,
                                "name": p.name_uz,
                                "price_uzs": float(p.price_uzs),
                                "in_stock": p.in_stock,
                            }
                            for p in matched[:5]
                        ]
                    }
        except Exception as exc:
            logger.warning("lookup_product_failed", error=str(exc))
            return {"products": [], "error": str(exc)}

    async def _tool_check_availability(self, sku: str) -> dict:
        from app.core.tenant_context import tenant_context
        from app.db.models.product import Product
        from app.db.session import get_tenant_session
        from sqlalchemy import select

        try:
            with tenant_context(self.tenant_id):
                async with get_tenant_session() as session:
                    result = await session.execute(
                        select(Product).where(
                            Product.tenant_id == self.tenant_id,
                            Product.sku == sku,
                        )
                    )
                    product = result.scalar_one_or_none()
                    if product is None:
                        return {"sku": sku, "found": False}
                    return {
                        "sku": sku,
                        "found": True,
                        "in_stock": product.in_stock,
                        "name": product.name_uz,
                        "price_uzs": float(product.price_uzs),
                    }
        except Exception as exc:
            logger.warning("check_availability_failed", error=str(exc))
            return {"sku": sku, "found": False, "error": str(exc)}

    async def _tool_create_order_draft(self, data: dict) -> dict:
        await self._log_action(
            "conversation.order_draft",
            {
                "sku": data.get("product_sku"),
                "phone": data.get("customer_phone"),
                "qty": data.get("quantity", 1),
            },
        )
        order_id = str(uuid.uuid4())
        return {
            "order_id": order_id,
            "status": "draft",
            "product_sku": data.get("product_sku"),
            "customer_phone": data.get("customer_phone"),
        }

    def _tool_suggest_quick_replies(self, data: dict) -> dict:
        options = [str(o)[:20] for o in data.get("options", [])][:4]
        return {"options": options, "status": "quick_replies_ready"}

    async def _tool_show_product_carousel(self, query: str) -> dict:
        """Look up products by query and return carousel-ready product dicts."""
        result = await self._tool_lookup_product(query)
        products = result.get("products", [])
        return {"products": products, "count": len(products)}

    async def _tool_suggest_telegram_handoff(self, data: dict) -> dict:
        """Build a Telegram CTA message with a personalised deep-link token.

        When a customer_id is available (i.e. the customer was identified on
        Instagram), a CustomerHandoff row is created and the token is embedded
        in the deep link so the Telegram /start handler can merge identities.
        """
        reason = data.get("reason", "")
        bot_username = self.agent_db_record.config_json.get("telegram_bot_username")
        username = bot_username.lstrip("@") if bot_username else None

        token: str | None = None
        customer_id = getattr(self, "_active_customer_id", None)
        if customer_id:
            try:
                from app.services.customer_service import CustomerService
                cs = CustomerService(self.db_session)
                handoff = await cs.create_handoff(
                    tenant_id=self.tenant_id,
                    customer_id=uuid.UUID(customer_id),
                )
                token = str(handoff.id)
            except Exception as exc:
                logger.warning("handoff_token_creation_failed", error=str(exc))

        if username and token:
            link = f"https://t.me/{username}?start={token}"
        elif username:
            link = f"https://t.me/{username}"
        else:
            link = None

        if link:
            message = (
                "For a more detailed conversation about your order, "
                f"I'd love to continue on Telegram! Tap here: {link}"
            )
        else:
            message = (
                "For a more detailed conversation and to finalise your order, "
                "please message us on Telegram — we can assist you much better there!"
            )
        return {"message": message, "reason": reason, "token": token}

    # ── Cost estimation ───────────────────────────────────────────────────────

    def _estimate_cost(self, resp) -> float:
        from app.integrations.anthropic_client import MODEL_COSTS
        costs = None
        for prefix, c in MODEL_COSTS.items():
            if self.model.startswith(prefix):
                costs = c
                break
        if costs is None:
            return 0.0
        usage = resp.usage
        return (
            usage.input_tokens * costs["input"]
            + usage.output_tokens * costs["output"]
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _content_to_dicts(content: list) -> list[dict]:
    result = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "text":
            result.append({"type": "text", "text": block.text})
        elif btype == "tool_use":
            result.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
        else:
            result.append({"type": btype or "unknown"})
    return result


def _extract_text(content: list) -> str | None:
    parts = [block.text for block in content if getattr(block, "type", None) == "text"]
    return "\n".join(parts) if parts else None


def _extract_text_from_history(history: list[dict]) -> str | None:
    for msg in reversed(history):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            if texts:
                return "\n".join(texts)
    return None
