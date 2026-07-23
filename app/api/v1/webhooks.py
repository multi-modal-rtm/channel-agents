"""Webhook endpoints for inbound channel traffic.

POST /webhooks/telegram/{tenant_slug}  — Telegram Bot API updates
GET  /webhooks/instagram               — Meta webhook verification challenge
POST /webhooks/instagram               — Meta / Instagram events
"""

from __future__ import annotations

import structlog

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_admin_session
from app.integrations.meta.webhook_handler import (
    process_instagram_webhook,
    verify_signature,
)
from app.integrations.telegram.client import TelegramClient
from app.integrations.telegram.models import TelegramUpdate
from app.integrations.telegram.webhook_handler import get_tenant_by_slug, process_update

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_fernet = Fernet(settings.encryption_key.encode())


# ── Telegram ──────────────────────────────────────────────────────────────────

@router.post("/telegram/{tenant_slug}", status_code=200)
async def telegram_webhook(tenant_slug: str, request: Request) -> Response:
    """
    Receive a Telegram Bot API update for the tenant identified by slug.

    Returns 200 immediately (Telegram requires fast ACK); heavy work is
    enqueued to the arq background worker.
    """
    body = await request.body()

    async with get_admin_session() as session:
        tenant = await get_tenant_by_slug(tenant_slug, session)
        if tenant is None:
            # Return 200 to avoid Telegram retrying with bad-slug spam
            return Response(status_code=200)

        # Verify webhook secret if the tenant has one configured
        if tenant.telegram_webhook_secret:
            try:
                expected_secret = _fernet.decrypt(tenant.telegram_webhook_secret).decode()
            except (InvalidToken, Exception):
                logger.warning("failed to decrypt webhook secret for tenant %s", tenant_slug)
                return Response(status_code=200)

            header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if not TelegramClient.verify_secret_token(header_secret, expected_secret):
                raise HTTPException(status_code=403, detail="invalid webhook secret")

        try:
            update = TelegramUpdate.model_validate(await request.json())
        except Exception:
            logger.warning("malformed telegram update for tenant %s", tenant_slug)
            return Response(status_code=200)

        arq_redis = getattr(request.app.state, "arq_redis", None)

        await process_update(
            update=update,
            tenant=tenant,
            session=session,
            arq_redis=arq_redis,
        )

    return Response(status_code=200)


# ── Instagram ─────────────────────────────────────────────────────────────────

@router.get("/instagram")
async def instagram_webhook_verify(request: Request) -> Response:
    """Meta webhook verification handshake."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and token == settings.meta_verify_token:
        return Response(content=challenge, media_type="text/plain")

    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/instagram", status_code=200)
async def instagram_webhook(request: Request) -> Response:
    """
    Receive Meta webhook events. Must return 200 within 1 second.
    All heavy work is enqueued to the arq background worker.
    """
    body = await request.body()

    sig = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(body, settings.meta_app_secret or "", sig):
        logger.warning("instagram_webhook_bad_signature")
        raise HTTPException(status_code=403, detail="invalid signature")

    try:
        payload = await request.json()
    except Exception:
        logger.warning("instagram_webhook_malformed_body")
        return Response(status_code=200)

    arq_redis = getattr(request.app.state, "arq_redis", None)

    async with get_admin_session() as session:
        await process_instagram_webhook(payload, session=session, arq_redis=arq_redis)

    return Response(status_code=200)
