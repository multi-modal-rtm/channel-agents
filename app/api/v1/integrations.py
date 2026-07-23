"""Instagram / Meta integration management endpoints.

GET  /integrations/instagram/connect      → returns OAuth URL
GET  /integrations/instagram/callback     → handles OAuth callback, stores token
GET  /integrations/instagram/status       → connection info
POST /integrations/instagram/disconnect   → revokes token, clears stored data
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentTenant, require_role
from app.core.security import decrypt_api_key, encrypt_api_key
from app.db.models.audit_log import AuditLog
from app.db.models.tenant import Tenant
from app.db.session import get_admin_session, get_tenant_session
from app.integrations.meta.oauth import (
    MetaOAuthError,
    exchange_code_for_token,
    exchange_for_long_lived_token,
    get_authorization_url,
    get_page_access_tokens,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/integrations", tags=["integrations"])

Owner = Annotated[type, Depends(require_role("owner"))]


# ── Response schemas ──────────────────────────────────────────────────────────

class InstagramConnectResponse(BaseModel):
    authorization_url: str


class InstagramStatusResponse(BaseModel):
    connected: bool
    page_id: str | None = None
    page_name: str | None = None
    token_expires_at: datetime | None = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/instagram/connect", response_model=InstagramConnectResponse)
async def instagram_connect(
    user: Owner,
    tenant: CurrentTenant,
    request: Request,
) -> InstagramConnectResponse:
    """Return the Meta OAuth URL. Redirect the user's browser to this URL."""
    redirect_uri = str(request.url_for("instagram_callback"))
    try:
        url = get_authorization_url(tenant.id, redirect_uri)
    except MetaOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return InstagramConnectResponse(authorization_url=url)


@router.get("/instagram/callback", name="instagram_callback")
async def instagram_callback(
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
) -> dict:
    """
    Meta redirects here after the user grants permissions.
    ``state`` carries the tenant_id set in get_authorization_url.

    This endpoint is unauthenticated because Meta calls it directly —
    the tenant is identified by the ``state`` parameter instead.
    """
    try:
        tenant_id = UUID(state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid state parameter") from exc

    redirect_uri = str(request.url_for("instagram_callback"))

    try:
        short_token = await exchange_code_for_token(code, redirect_uri)
        long_token, expires_in = await exchange_for_long_lived_token(short_token)
        pages = await get_page_access_tokens(long_token)
    except MetaOAuthError as exc:
        logger.warning("instagram_oauth_failed", tenant_id=str(tenant_id), error=str(exc))
        raise HTTPException(status_code=502, detail=f"Meta OAuth error: {exc}") from exc

    if not pages:
        raise HTTPException(status_code=422, detail="No Facebook pages found for this account")

    # Use the first page that has an instagram_business_account; fall back to first page.
    page = next(
        (p for p in pages if p.get("instagram_business_account")),
        pages[0],
    )
    page_id = page["id"]
    page_token = page.get("access_token", long_token)
    ig_account = page.get("instagram_business_account", {})
    instagram_user_id = ig_account.get("id")

    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    encrypted_token = encrypt_api_key(page_token)

    async with get_admin_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        db_tenant = result.scalar_one_or_none()
        if db_tenant is None:
            raise HTTPException(status_code=404, detail="Tenant not found")

        db_tenant.instagram_page_id = page_id
        db_tenant.instagram_page_access_token_encrypted = encrypted_token
        db_tenant.instagram_token_expires_at = expires_at
        db_tenant.instagram_user_id = instagram_user_id
        await session.commit()

    from app.core.tenant_context import tenant_context
    with tenant_context(tenant_id):
        async with get_tenant_session() as session:
            session.add(AuditLog(
                tenant_id=tenant_id,
                action="instagram.connect",
                entity_type="tenant",
                entity_id=tenant_id,
                payload_json={"page_id": page_id, "expires_at": expires_at.isoformat()},
            ))
            await session.commit()

    logger.info("instagram_connected", tenant_id=str(tenant_id), page_id=page_id)
    return {"status": "connected", "page_id": page_id}


@router.get("/instagram/status", response_model=InstagramStatusResponse)
async def instagram_status(tenant: CurrentTenant) -> InstagramStatusResponse:
    if not tenant.instagram_page_id:
        return InstagramStatusResponse(connected=False)

    return InstagramStatusResponse(
        connected=True,
        page_id=tenant.instagram_page_id,
        page_name=None,  # would require an extra Graph API call — omit for now
        token_expires_at=tenant.instagram_token_expires_at,
    )


@router.post("/instagram/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def instagram_disconnect(
    user: Owner,
    tenant: CurrentTenant,
) -> None:
    """Revoke stored tokens and clear Instagram integration data."""
    async with get_admin_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.id == tenant.id))
        db_tenant = result.scalar_one()
        db_tenant.instagram_page_id = None
        db_tenant.instagram_page_access_token_encrypted = None
        db_tenant.instagram_token_expires_at = None
        db_tenant.instagram_user_id = None
        await session.commit()

    from app.core.tenant_context import tenant_context
    with tenant_context(tenant.id):
        async with get_tenant_session() as session:
            session.add(AuditLog(
                tenant_id=tenant.id,
                action="instagram.disconnect",
                entity_type="tenant",
                entity_id=tenant.id,
                payload_json={},
            ))
            await session.commit()

    logger.info("instagram_disconnected", tenant_id=str(tenant.id))
