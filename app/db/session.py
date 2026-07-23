from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.core.tenant_context import get_current_tenant_id

engine = create_async_engine(
    settings.database_url_asyncpg,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    echo=settings.debug,
    connect_args=settings.database_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_tenant_session() -> AsyncGenerator[AsyncSession, None]:
    """Session with RLS GUC set from ContextVar (Rule 2 + 3).

    Raises RuntimeError if no tenant is in context — never allows unscoped queries
    on tenant-scoped tables (Rule 10: fail closed).
    """
    tenant_id = get_current_tenant_id()  # raises if not set
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"),
            {"tid": str(tenant_id)},
        )
        yield session


@asynccontextmanager
async def get_admin_session() -> AsyncGenerator[AsyncSession, None]:
    """Session WITHOUT the RLS GUC — for tenant management only.

    Allowed only for:
      - Tenant creation (no tenant context exists yet)
      - Platform-level admin operations
      - Reading the tenants table (no RLS)
    Never use for tenant-scoped table reads/writes without an explicit set_config call.
    """
    async with AsyncSessionLocal() as session:
        yield session


# ── FastAPI dependency shims (thin wrappers kept for backward compat) ─────────

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Admin session as a FastAPI dependency. Use only for non-tenant routes."""
    async with get_admin_session() as session:
        yield session


async def get_rls_db() -> AsyncGenerator[AsyncSession, None]:
    """Tenant session as a FastAPI dependency. Requires tenant context to be set."""
    async with get_tenant_session() as session:
        yield session
