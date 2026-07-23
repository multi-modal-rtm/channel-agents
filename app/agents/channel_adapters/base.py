"""Abstract base for all channel adapters.

An adapter's job is to transform agent-produced text into what a given
channel can actually render, and to produce channel-native payloads for
interactive elements (quick replies, carousels).

The agent is always channel-agnostic.  The adapter is applied in the
background task after the LLM has produced its response, before the
reply is sent over the wire.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class ChannelAdapter(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        """Canonical channel identifier, e.g. 'telegram' or 'instagram'."""
        ...

    @property
    @abstractmethod
    def max_length(self) -> int:
        """Hard character limit for a single message."""
        ...

    @abstractmethod
    def format_message(self, text: str) -> str:
        """Transform agent text for channel delivery.

        Implementations must:
          - Strip any formatting the channel cannot render.
          - Truncate to max_length (with trailing ellipsis if cut).
        """
        ...

    def format_quick_replies(self, text: str, options: list[str]) -> dict:
        """Return a channel-native payload for a message with quick-reply buttons.

        Default: ignore quick replies — the channel doesn't support them.
        Subclasses override for channels that do (Instagram, Messenger).
        """
        return {"text": self.format_message(text), "quick_replies": []}

    def format_product_carousel(self, products: list[dict]) -> dict:
        """Return a channel-native payload for a product carousel/list.

        Default: plain-text bullet list.  Subclasses override for richer formats.
        ``products`` is a list of dicts with at least ``name_uz`` and ``price_uzs``.
        """
        lines = []
        for p in products[:10]:
            name = p.get("name") or p.get("name_uz") or "Product"
            price = p.get("price_uzs", "")
            lines.append(f"• {name}: {price} UZS" if price else f"• {name}")
        return {"text": "\n".join(lines) if lines else "No products found."}

    # ── Shared utility ────────────────────────────────────────────────────────

    def _truncate(self, text: str) -> str:
        """Truncate to max_length with a single-char ellipsis when cut."""
        if len(text) <= self.max_length:
            return text
        return text[: self.max_length - 1] + "…"
