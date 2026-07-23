"""Meta / Instagram OAuth 2.0 helpers.

Flow:
  1. get_authorization_url()   → send user to Meta login
  2. exchange_code_for_token() → short-lived user access token (1-2 hours)
  3. exchange_for_long_lived_token() → 60-day token
  4. get_page_access_tokens()  → per-page tokens from user token

All HTTP calls use httpx with a 30-second timeout.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

_GRAPH = "https://graph.facebook.com/v20.0"
_DIALOG = "https://www.facebook.com/v20.0/dialog/oauth"
_TIMEOUT = httpx.Timeout(30.0)

# Scopes required for Instagram Messaging + comment management.
_SCOPES = [
    "instagram_basic",
    "instagram_manage_messages",
    "pages_manage_metadata",
    "pages_read_engagement",
]


class MetaOAuthError(Exception):
    pass


def get_authorization_url(tenant_id: UUID, redirect_uri: str) -> str:
    """Build the Meta OAuth dialog URL.

    The ``state`` parameter encodes the tenant_id so the callback knows
    which tenant completed the OAuth flow.
    """
    if not settings.meta_app_id:
        raise MetaOAuthError("META_APP_ID is not configured")

    params = {
        "client_id": settings.meta_app_id,
        "redirect_uri": redirect_uri,
        "scope": ",".join(_SCOPES),
        "state": str(tenant_id),
        "response_type": "code",
    }
    return f"{_DIALOG}?{urlencode(params)}"


async def exchange_code_for_token(
    code: str,
    redirect_uri: str,
    *,
    _http: httpx.AsyncClient | None = None,
) -> str:
    """Exchange an authorization code for a short-lived user access token."""
    _require_meta_credentials()

    params = {
        "client_id": settings.meta_app_id,
        "client_secret": settings.meta_app_secret,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    data = await _get(f"{_GRAPH}/oauth/access_token", params=params, _http=_http)
    token = data.get("access_token")
    if not token:
        raise MetaOAuthError(f"No access_token in response: {data}")
    return token


async def exchange_for_long_lived_token(
    short_lived_token: str,
    *,
    _http: httpx.AsyncClient | None = None,
) -> tuple[str, int]:
    """Exchange a short-lived user token for a 60-day token.

    Returns ``(token, expires_in_seconds)``.
    """
    _require_meta_credentials()

    params = {
        "grant_type": "fb_exchange_token",
        "client_id": settings.meta_app_id,
        "client_secret": settings.meta_app_secret,
        "fb_exchange_token": short_lived_token,
    }
    data = await _get(f"{_GRAPH}/oauth/access_token", params=params, _http=_http)
    token = data.get("access_token")
    if not token:
        raise MetaOAuthError(f"No access_token in long-lived exchange response: {data}")
    expires_in = int(data.get("expires_in", 5183944))  # default ~60 days
    return token, expires_in


async def get_page_access_tokens(
    user_token: str,
    *,
    _http: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Return a list of pages the user manages, each with its page access token.

    Each item: ``{"id": "...", "name": "...", "access_token": "...", "instagram_business_account": {...}}``
    """
    data = await _get(
        f"{_GRAPH}/me/accounts",
        params={
            "access_token": user_token,
            "fields": "id,name,access_token,instagram_business_account",
        },
        _http=_http,
    )
    return data.get("data", [])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_meta_credentials() -> None:
    if not settings.meta_app_id or not settings.meta_app_secret:
        raise MetaOAuthError("META_APP_ID and META_APP_SECRET must be configured")


async def _get(
    url: str,
    params: dict[str, Any],
    *,
    _http: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    if _http is not None:
        resp = await _http.get(url, params=params)
    else:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            resp = await http.get(url, params=params)

    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", resp.json())
        except Exception:
            detail = {"raw": resp.text}
        raise MetaOAuthError(f"Meta API {resp.status_code}: {detail}")

    return resp.json()
