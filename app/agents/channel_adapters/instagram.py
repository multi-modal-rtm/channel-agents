"""InstagramAdapter — plain text only, 1000 char limit, quick replies + carousels."""

from __future__ import annotations

import re

from app.agents.channel_adapters.base import ChannelAdapter

_MAX_LENGTH = 1000      # Instagram Messaging API limit per message
_MAX_QUICK_REPLIES = 13  # Platform limit for quick reply buttons
_QUICK_REPLY_TITLE_MAX = 20  # Characters per quick reply title

# Ordered from most to least greedy so nested marks resolve cleanly.
_MARKDOWN_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"```[\s\S]*?```"),          lambda m: m.group(0).strip("`").strip()),
    (re.compile(r"`(.+?)`"),                  r"\1"),
    (re.compile(r"\*\*(.+?)\*\*"),            r"\1"),
    (re.compile(r"__(.+?)__"),                r"\1"),
    (re.compile(r"\*(.+?)\*"),                r"\1"),
    (re.compile(r"_(.+?)_"),                  r"\1"),
    (re.compile(r"~~(.+?)~~"),                r"\1"),
    (re.compile(r"^\s*#{1,6}\s+", re.M),      ""),
    (re.compile(r"\[(.+?)\]\(.+?\)"),         r"\1"),
]


def _strip_markdown(text: str) -> str:
    for pattern, replacement in _MARKDOWN_PATTERNS:
        if callable(replacement):
            text = pattern.sub(replacement, text)
        else:
            text = pattern.sub(replacement, text)
    return text


class InstagramAdapter(ChannelAdapter):

    @property
    def name(self) -> str:
        return "instagram"

    @property
    def max_length(self) -> int:
        return _MAX_LENGTH

    def format_message(self, text: str) -> str:
        """Strip Markdown and enforce the 1000-character limit."""
        return self._truncate(_strip_markdown(text))

    def format_quick_replies(self, text: str, options: list[str]) -> dict:
        """Build an Instagram quick-reply payload (up to 13 buttons).

        Returns a dict ready to be passed as the ``message`` body to the
        Instagram Messaging API.
        """
        quick_replies = [
            {
                "content_type": "text",
                "title": opt[:_QUICK_REPLY_TITLE_MAX],
                "payload": opt,
            }
            for opt in options[:_MAX_QUICK_REPLIES]
        ]
        return {
            "text": self.format_message(text),
            "quick_replies": quick_replies,
        }

    def format_product_carousel(self, products: list[dict]) -> dict:
        """Build an Instagram Generic Template carousel payload.

        Falls back to a plain-text list when the product list is empty.
        """
        elements = []
        for p in products[:10]:
            name = str(p.get("name") or p.get("name_uz") or "Product")[:80]
            price = p.get("price_uzs", "")
            subtitle = f"{price} UZS" if price else ""
            sku = p.get("sku", "")
            elements.append({
                "title": name,
                "subtitle": subtitle,
                "buttons": [
                    {
                        "type": "postback",
                        "title": "Order",
                        "payload": f"ORDER_{sku}",
                    },
                ],
            })

        if not elements:
            return {"text": "No products found."}

        return {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "generic",
                    "elements": elements,
                },
            }
        }
