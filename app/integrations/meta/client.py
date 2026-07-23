"""Instagram Graph API client.

Single chokepoint for all outbound Instagram calls. Handles:
  - Token loading (decrypts stored page access token)
  - send_message, reply_to_comment, get_user_profile
  - Exponential-backoff retry on 429 / 5xx (max 3 attempts)
  - Rate-limit metadata via X-Business-Use-Case-Usage header
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

import httpx
import structlog

from app.core.security import decrypt_api_key
from app.db.models.tenant import Tenant
from app.db.session import get_admin_session

logger = structlog.get_logger(__name__)

_GRAPH_API = "https://graph.facebook.com/v20.0"
_TIMEOUT = httpx.Timeout(30.0)
_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0  # seconds


class InstagramAPIError(Exception):
    def __init__(self, status_code: int, detail: dict[str, Any]) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Instagram API error {status_code}: {detail}")


class InstagramClient:
    """
    Instantiate via ``await InstagramClient.create(tenant_id)`` in production.
    Pass ``_page_id``, ``_access_token``, and optionally ``_http`` in tests
    to bypass DB loading.
    """

    def __init__(
        self,
        *,
        page_id: str,
        access_token: str,
        _http: httpx.AsyncClient | None = None,
        _backoff_base: float = _BACKOFF_BASE,
    ) -> None:
        self._page_id = page_id
        self._token = access_token
        self._http = _http
        self._backoff_base = _backoff_base

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    async def create(cls, tenant_id: UUID) -> "InstagramClient":
        async with get_admin_session() as session:
            from sqlalchemy import select
            result = await session.execute(
                select(Tenant).where(Tenant.id == tenant_id)
            )
            tenant = result.scalar_one_or_none()

        if tenant is None:
            raise ValueError(f"Tenant {tenant_id} not found")
        if not tenant.instagram_page_id:
            raise ValueError(f"Tenant {tenant_id} has no Instagram page connected")
        if not tenant.instagram_page_access_token_encrypted:
            raise ValueError(f"Tenant {tenant_id} has no Instagram access token stored")

        token = decrypt_api_key(tenant.instagram_page_access_token_encrypted)
        return cls(page_id=tenant.instagram_page_id, access_token=token)

    # ── Public API ────────────────────────────────────────────────────────────

    async def send_message(self, recipient_id: str, text: str) -> dict[str, Any]:
        """Send a direct message to a user via the Instagram Messaging API."""
        return await self._request(
            "POST",
            f"/{self._page_id}/messages",
            json={
                "recipient": {"id": recipient_id},
                "message": {"text": text},
                "messaging_type": "RESPONSE",
            },
        )

    async def send_structured_message(
        self,
        recipient_id: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a structured message payload (quick replies, generic templates).

        ``message`` is the ``message`` sub-dict of the Messenger/Instagram send
        API request — e.g. ``{"text": "...", "quick_replies": [...]}`` or
        ``{"attachment": {"type": "template", "payload": {...}}}``.
        """
        return await self._request(
            "POST",
            f"/{self._page_id}/messages",
            json={
                "recipient": {"id": recipient_id},
                "message": message,
                "messaging_type": "RESPONSE",
            },
        )

    async def reply_to_comment(self, comment_id: str, text: str) -> dict[str, Any]:
        """Post a reply to an Instagram comment."""
        return await self._request(
            "POST",
            f"/{comment_id}/replies",
            json={"message": text},
        )

    async def get_user_profile(self, user_id: str) -> dict[str, Any]:
        """Fetch basic public profile info for a user (best-effort; may be limited)."""
        return await self._request(
            "GET",
            f"/{user_id}",
            params={"fields": "name,profile_pic"},
        )

    # ── Internals ────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Execute a Graph API request with retry on 429 / 5xx."""
        url = f"{_GRAPH_API}{path}"
        query = {"access_token": self._token, **(params or {})}

        for attempt in range(_MAX_RETRIES):
            response = await self._send(method, url, params=query, json=json)

            _log_rate_limit(response)

            if response.status_code == 429:
                if attempt < _MAX_RETRIES - 1:
                    delay = self._backoff_base ** (attempt + 1)
                    logger.warning(
                        "instagram_rate_limited",
                        attempt=attempt + 1,
                        retry_in=delay,
                        path=path,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise InstagramAPIError(429, {"message": "rate limit exceeded"})

            if response.status_code >= 500:
                if attempt < _MAX_RETRIES - 1:
                    delay = self._backoff_base ** (attempt + 1)
                    logger.warning(
                        "instagram_server_error",
                        status=response.status_code,
                        attempt=attempt + 1,
                        retry_in=delay,
                        path=path,
                    )
                    await asyncio.sleep(delay)
                    continue

            if response.status_code >= 400:
                try:
                    detail = response.json().get("error", response.json())
                except Exception:
                    detail = {"raw": response.text}
                raise InstagramAPIError(response.status_code, detail)

            return response.json()

        raise InstagramAPIError(500, {"message": "all retries exhausted"})

    async def _send(
        self,
        method: str,
        url: str,
        params: dict[str, Any],
        json: dict[str, Any] | None,
    ) -> httpx.Response:
        if self._http is not None:
            return await self._http.request(method, url, params=params, json=json)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            return await http.request(method, url, params=params, json=json)


def _log_rate_limit(response: httpx.Response) -> None:
    usage = response.headers.get("X-Business-Use-Case-Usage")
    if usage:
        logger.debug("instagram_rate_limit_usage", usage=usage)
