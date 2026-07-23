"""TelegramClient — thin httpx wrapper around the Bot API.

Uses httpx (already a project dependency) instead of aiogram or
python-telegram-bot to keep the dependency tree minimal.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}"


class TelegramClient:
    def __init__(self, bot_token: str) -> None:
        self._token = bot_token
        self._base = _TELEGRAM_API.format(token=bot_token)

    # ── Outbound ──────────────────────────────────────────────────────────────

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
    ) -> dict:
        payload: dict = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        if parse_mode:
            payload["parse_mode"] = parse_mode

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{self._base}/sendMessage", json=payload)
            resp.raise_for_status()
            return resp.json()

    # ── Webhook registration ──────────────────────────────────────────────────

    async def set_webhook(self, url: str, *, secret_token: str | None = None) -> dict:
        payload: dict = {"url": url}
        if secret_token:
            payload["secret_token"] = secret_token
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{self._base}/setWebhook", json=payload)
            resp.raise_for_status()
            return resp.json()

    async def delete_webhook(self) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{self._base}/deleteWebhook")
            resp.raise_for_status()
            return resp.json()

    # ── Security ─────────────────────────────────────────────────────────────

    @staticmethod
    def verify_secret_token(header_value: str | None, expected_secret: str) -> bool:
        """Constant-time comparison of X-Telegram-Bot-Api-Secret-Token header."""
        if not header_value:
            return False
        return hmac.compare_digest(
            header_value.encode(),
            expected_secret.encode(),
        )
