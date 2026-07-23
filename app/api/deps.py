from collections.abc import AsyncGenerator, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.core.tenant_context import clear_current_tenant_id, set_current_tenant_id
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.session import get_admin_session, get_db, get_rls_db, get_tenant_session

bearer = HTTPBearer()

_401 = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
_403 = HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
) -> AsyncGenerator[User, None]:
    """Parse JWT, set tenant context, verify user exists via RLS-backed query.

    Sets app.tenant_id ContextVar for the duration of the request (Rule 3).
    Clears it in finally so it never leaks to the next request on the same task.
    """
    try:
        payload = decode_token(credentials.credentials)
    except JWTError as exc:
        raise _401 from exc

    if payload.get("type") != "access":
        raise _401

    try:
        tenant_id = UUID(payload["tenant_id"])
        user_id = UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise _401 from exc

    # Rule 3: set ContextVar before any DB access
    set_current_tenant_id(tenant_id)
    from app.core.logging import bind_tenant, clear_context
    bind_tenant(str(tenant_id))
    try:
        # RLS filters to tenant_id == GUC — tampered tenant_id → no row returned
        async with get_tenant_session() as session:
            result = await session.execute(
                select(User).where(User.id == user_id, User.is_active == True)  # noqa: E712
            )
            user = result.scalar_one_or_none()

        if user is None:
            raise _401

        # Defence-in-depth: explicit tenant check in addition to RLS (Rule 10).
        # Catches tampered JWTs even when the DB role has BYPASSRLS.
        if user.tenant_id != tenant_id:
            raise _401

        yield user
    finally:
        clear_current_tenant_id()
        clear_context()


async def get_current_tenant(
    user: Annotated[User, Depends(get_current_user)],
) -> Tenant:
    """Return the current tenant; raise 403 if suspended (Rule 8 kill switch)."""
    async with get_admin_session() as session:
        result = await session.execute(select(Tenant).where(Tenant.id == user.tenant_id))
        tenant = result.scalar_one_or_none()

    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if tenant.status in ("suspended", "cancelled"):
        raise HTTPException(status_code=403, detail="Tenant account is suspended")
    return tenant


def require_role(*allowed_roles: str) -> Callable:
    """Dependency factory: raises 403 if the current user's role is not in allowed_roles."""
    async def _check(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in allowed_roles:
            raise _403
        return user
    return _check


# ── Typed aliases ─────────────────────────────────────────────────────────────
CurrentUser = Annotated[User, Depends(get_current_user)]
CurrentTenant = Annotated[Tenant, Depends(get_current_tenant)]
RlsDbSession = Annotated[AsyncSession, Depends(get_rls_db)]
AdminDbSession = Annotated[AsyncSession, Depends(get_db)]
