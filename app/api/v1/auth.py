import uuid

from fastapi import APIRouter, HTTPException, status
from jose import JWTError
from sqlalchemy import select, text

from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.session import get_admin_session
from app.schemas.auth import LoginRequest, RefreshRequest, TenantRegisterRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register/tenant", status_code=status.HTTP_201_CREATED)
async def register_tenant(body: TenantRegisterRequest) -> TokenResponse:
    """Create a new tenant and its first owner user in a single transaction."""
    async with get_admin_session() as session:
        # Uniqueness checks
        slug_taken = await session.scalar(select(Tenant.id).where(Tenant.slug == body.slug))
        if slug_taken:
            raise HTTPException(status_code=400, detail="Slug already taken")

        email_taken = await session.scalar(select(User.id).where(User.email == body.email))
        if email_taken:
            raise HTTPException(status_code=400, detail="Email already registered")

        # Create tenant (tenants table has no RLS)
        tenant = Tenant(
            id=uuid.uuid4(),
            name=body.name,
            slug=body.slug,
            plan="starter",
            billing_mode="managed",
            status="trial",
        )
        session.add(tenant)
        await session.flush()  # assign tenant.id before user insert

        # Set GUC so FORCE ROW LEVEL SECURITY allows the user insert
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant.id)},
        )

        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email=body.email,
            password_hash=hash_password(body.password),
            role="owner",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

    return TokenResponse(
        access_token=create_access_token(user.id, tenant.id, user.role),
        refresh_token=create_refresh_token(user.id, tenant.id),
    )


@router.post("/login")
async def login(body: LoginRequest) -> TokenResponse:
    # email is not globally unique — must search without a tenant filter
    # use admin session (no RLS), then verify the user belongs to an active tenant
    async with get_admin_session() as session:
        result = await session.execute(
            select(User).where(User.email == body.email, User.is_active == True)  # noqa: E712
        )
        user = result.scalar_one_or_none()

    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Check tenant is not suspended
    async with get_admin_session() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == user.tenant_id))

    if tenant is None or tenant.status in ("suspended", "cancelled"):
        raise HTTPException(status_code=403, detail="Tenant account is suspended")

    return TokenResponse(
        access_token=create_access_token(user.id, user.tenant_id, user.role),
        refresh_token=create_refresh_token(user.id, user.tenant_id),
    )


@router.post("/refresh")
async def refresh_token(body: RefreshRequest) -> TokenResponse:
    try:
        payload = decode_token(body.refresh_token)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid refresh token") from exc

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Not a refresh token")

    user_id = payload["sub"]
    tenant_id = payload["tenant_id"]

    # Re-verify user is still active
    async with get_admin_session() as session:
        user = await session.scalar(select(User).where(User.id == user_id, User.is_active == True))  # noqa: E712

    if user is None:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    return TokenResponse(
        access_token=create_access_token(user_id, tenant_id, user.role),
        refresh_token=create_refresh_token(user_id, tenant_id),
    )
