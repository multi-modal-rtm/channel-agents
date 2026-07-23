"""
Root test conftest. Sets minimum required env vars before any app module is imported
so pydantic-settings can initialise without a .env file present in CI.
"""

import os
import uuid

# Must be set before any `from app.*` import triggers Settings()
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest-not-for-prod-abc!")
# Valid Fernet key: base64url(b"x"*32)
os.environ.setdefault("ENCRYPTION_KEY", "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/textile_agents",
)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.main import app  # noqa: E402 — settings now available

TENANT_A_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
TENANT_B_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(scope="session")
def tenant_a_id() -> uuid.UUID:
    return TENANT_A_ID


@pytest.fixture(scope="session")
def tenant_b_id() -> uuid.UUID:
    return TENANT_B_ID


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
