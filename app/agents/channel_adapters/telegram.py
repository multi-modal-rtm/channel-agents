"""TelegramAdapter — Telegram supports MarkdownV1 and long messages."""

from __future__ import annotations

from app.agents.channel_adapters.base import ChannelAdapter

_MAX_LENGTH = 4096  # Telegram Bot API hard limit


class TelegramAdapter(ChannelAdapter):

    @property
    def name(self) -> str:
        return "telegram"

    @property
    def max_length(self) -> int:
        return _MAX_LENGTH

    def format_message(self, text: str) -> str:
        """Telegram supports Markdown; just enforce the length limit."""
        return self._truncate(text)

    def format_product_carousel(self, products: list[dict]) -> dict:
        """Format as a Markdown bullet list (Telegram renders *bold*)."""
        lines = []
        for p in products[:10]:
            name = p.get("name") or p.get("name_uz") or "Product"
            price = p.get("price_uzs", "")
            lines.append(f"• *{name}*: {price} UZS" if price else f"• *{name}*")
        return {"text": self._truncate("\n".join(lines)) if lines else "No products found."}
