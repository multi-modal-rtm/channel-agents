"""
Integration tests — tenant context plumbing.

Tests:
  1. Login flow returns valid JWT with correct tenant_id
  2. Tampered JWT (wrong tenant_id) is rejected — RLS blocks user lookup
  3. BYOK encrypt→decrypt roundtrip
  4. Suspended tenant cannot access protected endpoints
"""

import uuid

from httpx import AsyncClient
from jose import jwt

from app.config import settings
from app.core.security import (
    create_access_token,
    decrypt_api_key,
    encrypt_api_key,
)
from tests.integration.conftest import TENANT_A_ID, TENANT_B_ID, USER_A_ID, USER_PW

# ── 1. Login flow ─────────────────────────────────────────────────────────────

async def test_login_returns_jwt_with_correct_tenant(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/login", json={
        "email": "owner@alpha-int.com",
        "password": USER_PW,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "access_token" in body
    assert "refresh_token" in body
    assert body["token_type"] == "bearer"

    payload = jwt.decode(body["access_token"], settings.secret_key, algorithms=[settings.jwt_algorithm])
    assert payload["sub"] == str(USER_A_ID)
    assert payload["tenant_id"] == str(TENANT_A_ID)
    assert payload["role"] == "owner"
    assert payload["type"] == "access"


async def test_login_wrong_password_rejected(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/login", json={
        "email": "owner@alpha-int.com",
        "password": "wrongpassword",
    })
    assert resp.status_code == 401


async def test_refresh_token_issues_new_access_token(client: AsyncClient) -> None:
    login = await client.post("/api/v1/auth/login", json={
        "email": "owner@alpha-int.com",
        "password": USER_PW,
    })
    refresh_tok = login.json()["refresh_token"]

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_tok})
    assert resp.status_code == 200
    new_payload = jwt.decode(
        resp.json()["access_token"], settings.secret_key, algorithms=[settings.jwt_algorithm]
    )
    assert new_payload["type"] == "access"
    assert new_payload["tenant_id"] == str(TENANT_A_ID)


# ── 2. Cross-tenant access rejection ─────────────────────────────────────────

async def test_tampered_tenant_id_rejected(client: AsyncClient) -> None:
    """JWT claiming user A belongs to tenant B — RLS user lookup returns nothing → 401."""
    tampered = create_access_token(
        user_id=USER_A_ID,
        tenant_id=TENANT_B_ID,   # wrong tenant
        role="owner",
    )
    resp = await client.get(
        "/api/v1/tenants/me",
        headers={"Authorization": f"Bearer {tampered}"},
    )
    assert resp.status_code == 401


async def test_get_tenants_me_returns_own_tenant(client: AsyncClient) -> None:
    login = await client.post("/api/v1/auth/login", json={
        "email": "owner@alpha-int.com",
        "password": USER_PW,
    })
    token = login.json()["access_token"]

    resp = await client.get(
        "/api/v1/tenants/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(TENANT_A_ID)
    assert data["slug"] == "alpha-int"
    assert data["has_anthropic_key"] is False


# ── 3. BYOK encryption roundtrip ─────────────────────────────────────────────

def test_encrypt_decrypt_roundtrip() -> None:
    plaintext = "sk-ant-test-key-abc123"
    ciphertext = encrypt_api_key(plaintext)
    assert isinstance(ciphertext, bytes)
    assert ciphertext != plaintext.encode()
    assert decrypt_api_key(ciphertext) == plaintext


def test_different_plaintexts_produce_different_ciphertexts() -> None:
    c1 = encrypt_api_key("sk-ant-aaa")
    c2 = encrypt_api_key("sk-ant-bbb")
    assert c1 != c2


def test_fernet_ciphertext_is_opaque() -> None:
    """Ciphertext must not contain the plaintext (even as substring)."""
    plaintext = "sk-ant-supersecret"
    ciphertext = encrypt_api_key(plaintext)
    assert plaintext.encode() not in ciphertext


# ── 4. Suspended tenant blocked ───────────────────────────────────────────────

async def test_suspended_tenant_cannot_access_endpoints(client: AsyncClient) -> None:
    # Get a valid token first
    login = await client.post("/api/v1/auth/login", json={
        "email": "owner@beta-int.com",
        "password": USER_PW,
    })
    token = login.json()["access_token"]

    # Suspend tenant B directly via admin session
    from sqlalchemy import text

    import app.db.session as db_mod
    async with db_mod.AsyncSessionLocal() as session:
        await session.execute(
            text("UPDATE tenants SET status = 'suspended' WHERE id = :tid"),
            {"tid": str(TENANT_B_ID)},
        )
        await session.commit()

    try:
        resp = await client.get(
            "/api/v1/tenants/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
    finally:
        # Restore so other tests aren't affected
        async with db_mod.AsyncSessionLocal() as session:
            await session.execute(
                text("UPDATE tenants SET status = 'active' WHERE id = :tid"),
                {"tid": str(TENANT_B_ID)},
            )
            await session.commit()


# ── 5. Register new tenant ────────────────────────────────────────────────────

async def test_register_tenant_creates_owner_and_returns_jwt(client: AsyncClient) -> None:
    unique = uuid.uuid4().hex[:8]
    resp = await client.post("/api/v1/auth/register/tenant", json={
        "name": f"Test Tenant {unique}",
        "slug": f"test-{unique}",
        "email": f"owner@{unique}.com",
        "password": "securepass123",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()

    payload = jwt.decode(body["access_token"], settings.secret_key, algorithms=[settings.jwt_algorithm])
    assert payload["role"] == "owner"
    assert payload["type"] == "access"

    # Can immediately access /tenants/me with the issued token
    me = await client.get(
        "/api/v1/tenants/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["slug"] == f"test-{unique}"


async def test_register_duplicate_slug_rejected(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/register/tenant", json={
        "name": "Duplicate",
        "slug": "alpha-int",   # already seeded
        "email": "new@example.com",
        "password": "securepass123",
    })
    assert resp.status_code == 400
