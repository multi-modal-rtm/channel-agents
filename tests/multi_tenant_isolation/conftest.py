"""
Session-scoped fixtures for RLS isolation tests.

Sets up a dedicated test database (channel_agents_test), creates all tables via
SQLAlchemy, applies RLS policies via raw SQL, creates a non-superuser app role,
and seeds two test tenants. Each test gets admin and app connections.

Requires a running PostgreSQL instance. Provide credentials via env var:
  $env:PG_TEST_ADMIN_DSN="postgresql://postgres:YOURPASSWORD@localhost:5432/postgres"

Default assumes docker-compose.dev.yml is up (postgres:postgres).
"""

import asyncio
import os
import uuid
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

import app.db.models  # noqa: F401 — registers all ORM mappers
from app.db.base import Base

# ── Connection strings ────────────────────────────────────────────────────────
# Override: $env:PG_TEST_ADMIN_DSN="postgresql://postgres:YOURPASS@localhost:5432/postgres"
_ADMIN_RAW = os.getenv("PG_TEST_ADMIN_DSN", "postgresql://postgres:postgres@localhost:5432/postgres")
_parsed = urlparse(_ADMIN_RAW)
TEST_DB = os.getenv("PG_TEST_DB", "channel_agents_test")

ADMIN_DSN: str = _ADMIN_RAW                                        # system db (create test db here)
TEST_DSN: str = urlunparse(_parsed._replace(path=f"/{TEST_DB}"))   # test db, superuser
TEST_ASYNC_DSN: str = TEST_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)

APP_ROLE = "rls_app_user"
APP_PASSWORD = "rls_app_secret"
APP_DSN: str = urlunparse(
    _parsed._replace(
        netloc=f"{APP_ROLE}:{APP_PASSWORD}@{_parsed.hostname}:{_parsed.port or 5432}",
        path=f"/{TEST_DB}",
    )
)

TENANT_SCOPED_TABLES = [
    "users", "agents", "conversations", "messages",
    "products", "knowledge_docs", "ad_campaigns", "llm_calls", "audit_log",
]

# Module-level state populated once during session setup.
TENANT_A: uuid.UUID = uuid.uuid4()
TENANT_B: uuid.UUID = uuid.uuid4()


# ── Sync session fixture — asyncio.run() is safe outside any event loop ───────

@pytest.fixture(scope="session", autouse=True)
def _db_session() -> None:
    """Create test DB, schema, RLS policies, app role, seed tenants."""
    try:
        asyncio.run(_setup())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"PostgreSQL unavailable. Set PG_TEST_ADMIN_DSN and retry. ({exc})"
        )
    yield
    asyncio.run(_teardown())


async def _setup() -> None:
    # ── Create test database if absent ────────────────────────────────────────
    sys_conn = await asyncpg.connect(ADMIN_DSN)
    exists = await sys_conn.fetchval(
        "SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB
    )
    if not exists:
        await sys_conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    await sys_conn.close()

    # ── Drop and recreate tables ───────────────────────────────────────────────
    engine = create_async_engine(TEST_ASYNC_DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    # ── Apply RLS + create app role ────────────────────────────────────────────
    adm = await asyncpg.connect(TEST_DSN)
    try:
        await adm.execute(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                    CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}';
                END IF;
            END $$;
        """)
        await adm.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public"
            f" TO {APP_ROLE}"
        )
        for table in TENANT_SCOPED_TABLES:
            await adm.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            await adm.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            await adm.execute(f"""
                CREATE POLICY tenant_isolation ON {table}
                    USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
            """)
        await adm.execute(
            """
            INSERT INTO tenants (id, name, slug, plan, billing_mode, status)
            VALUES ($1, 'Alpha Textile', $2, 'starter', 'managed', 'active'),
                   ($3, 'Beta  Textile', $4, 'starter', 'managed', 'active')
            """,
            TENANT_A, f"alpha-{TENANT_A}",
            TENANT_B, f"beta-{TENANT_B}",
        )
    finally:
        await adm.close()


async def _teardown() -> None:
    engine = create_async_engine(TEST_ASYNC_DSN)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ── Per-test fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def tenant_a() -> uuid.UUID:
    return TENANT_A


@pytest.fixture(scope="session")
def tenant_b() -> uuid.UUID:
    return TENANT_B


@pytest_asyncio.fixture
async def adm() -> asyncpg.Connection:
    """Superuser connection — bypasses RLS for setup/teardown."""
    conn = await asyncpg.connect(TEST_DSN)
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def app() -> asyncpg.Connection:
    """Non-superuser connection — subject to RLS."""
    conn = await asyncpg.connect(APP_DSN)
    yield conn
    await conn.close()
