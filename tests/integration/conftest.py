"""
Integration test setup: spins up channel_agents_test on the local PG instance,
patches app.db.session to use it, seeds two tenants + users, provides an httpx client.

Requires: $env:PG_TEST_ADMIN_DSN="postgresql://postgres:PASSWORD@localhost:5432/postgres"
"""

import asyncio
import os
import uuid
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401 — registers all ORM mappers
from app.core.security import hash_password
from app.db.base import Base

# ── DSNs ─────────────────────────────────────────────────────────────────────
_ADMIN_RAW = os.getenv("PG_TEST_ADMIN_DSN", "postgresql://postgres:postgres@localhost:5432/postgres")
_parsed = urlparse(_ADMIN_RAW)
TEST_DB = os.getenv("PG_TEST_DB", "channel_agents_test")

ADMIN_DSN: str = _ADMIN_RAW
TEST_DSN: str = urlunparse(_parsed._replace(path=f"/{TEST_DB}"))
TEST_ASYNC_DSN: str = TEST_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)

TENANT_SCOPED_TABLES = [
    "users", "agents", "conversations", "messages",
    "products", "knowledge_docs", "ad_campaigns", "llm_calls", "audit_log",
]

# ── Fixed IDs so tests can reference them ─────────────────────────────────────
TENANT_A_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TENANT_B_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A_ID   = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_B_ID   = uuid.UUID("22222222-2222-2222-2222-222222222222")
USER_PW     = "password123"


# ── One-time DB setup ─────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def _integration_db() -> None:
    try:
        asyncio.run(_setup())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable for integration tests ({exc})")
    yield
    asyncio.run(_teardown())


async def _setup() -> None:
    sys_conn = await asyncpg.connect(ADMIN_DSN)
    exists = await sys_conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB)
    if not exists:
        await sys_conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    await sys_conn.close()

    engine = create_async_engine(TEST_ASYNC_DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    adm = await asyncpg.connect(TEST_DSN)
    try:
        await adm.execute("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rls_app_user') THEN
                    CREATE ROLE rls_app_user LOGIN PASSWORD 'rls_app_secret';
                END IF;
            END $$;
        """)
        await adm.execute(
            "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO rls_app_user"
        )
        for tbl in TENANT_SCOPED_TABLES:
            await adm.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
            await adm.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
            await adm.execute(f"""
                DO $$ BEGIN
                    IF EXISTS (
                        SELECT 1 FROM pg_policies WHERE tablename='{tbl}' AND policyname='tenant_isolation'
                    ) THEN DROP POLICY tenant_isolation ON {tbl}; END IF;
                END $$;
            """)
            await adm.execute(f"""
                CREATE POLICY tenant_isolation ON {tbl}
                    USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            """)

        pw = hash_password(USER_PW)
        await adm.execute("""
            INSERT INTO tenants (id, name, slug, plan, billing_mode, status)
            VALUES ($1,'Alpha Textile','alpha-int','starter','managed','active'),
                   ($2,'Beta  Textile','beta-int', 'starter','managed','active')
            ON CONFLICT DO NOTHING
        """, TENANT_A_ID, TENANT_B_ID)

        # Superuser bypasses FORCE RLS — direct inserts work
        await adm.execute("""
            INSERT INTO users (id, tenant_id, email, password_hash, role, is_active)
            VALUES ($1,$2,'owner@alpha-int.com',$3,'owner',true),
                   ($4,$5,'owner@beta-int.com', $6,'owner',true)
            ON CONFLICT DO NOTHING
        """, USER_A_ID, TENANT_A_ID, pw, USER_B_ID, TENANT_B_ID, pw)
    finally:
        await adm.close()


async def _teardown() -> None:
    engine = create_async_engine(TEST_ASYNC_DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ── Test engine + session factory ─────────────────────────────────────────────

@pytest.fixture(scope="session")
def test_engine(_integration_db):
    return create_async_engine(TEST_ASYNC_DSN)


@pytest.fixture(scope="session")
def test_session_factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
def _patch_db(test_engine, test_session_factory):
    """Redirect app.db.session to the test DB for the entire session."""
    import app.db.session as db_mod
    orig_engine = db_mod.engine
    orig_session = db_mod.AsyncSessionLocal
    db_mod.engine = test_engine
    db_mod.AsyncSessionLocal = test_session_factory
    yield
    db_mod.engine = orig_engine
    db_mod.AsyncSessionLocal = orig_session


# ── HTTP client ───────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client():
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.main import app

    mock_arq = AsyncMock()
    mock_arq.enqueue_job = AsyncMock()
    mock_arq.close = AsyncMock()

    with patch("app.main.create_pool", return_value=mock_arq):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


# ── Convenience fixtures ──────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def tenant_a_id() -> uuid.UUID:
    return TENANT_A_ID


@pytest.fixture(scope="session")
def tenant_b_id() -> uuid.UUID:
    return TENANT_B_ID


@pytest.fixture(scope="session")
def user_a_id() -> uuid.UUID:
    return USER_A_ID


@pytest.fixture(scope="session")
def user_b_id() -> uuid.UUID:
    return USER_B_ID
