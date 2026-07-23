"""
RLS isolation tests — MANDATORY CI GATE (Rule 9).

Tests prove cross-tenant data leaks are impossible at the database layer.
All mutations go through the `app` fixture (non-superuser, RLS-enforced).
The `adm` fixture (superuser) is used only for test setup/cleanup.

Run:  pytest tests/multi_tenant_isolation/ -v
"""

import uuid

import asyncpg
import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

async def _set_tenant(conn: asyncpg.Connection, tenant_id: uuid.UUID) -> None:
    """Set the RLS context GUC for the current transaction."""
    await conn.execute(
        "SELECT set_config('app.tenant_id', $1, true)", str(tenant_id)
    )


async def _insert_product(
    conn: asyncpg.Connection,
    tenant_id: uuid.UUID,
    sku: str,
    name: str = "Test product",
) -> uuid.UUID:
    pid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO products (id, tenant_id, sku, name_uz, price_uzs) VALUES ($1, $2, $3, $4, 50000)",
        pid, tenant_id, sku, name,
    )
    return pid


# ── 1. SELECT isolation ───────────────────────────────────────────────────────

async def test_select_only_own_tenant_rows(
    adm: asyncpg.Connection,
    app: asyncpg.Connection,
    tenant_a: uuid.UUID,
    tenant_b: uuid.UUID,
) -> None:
    """Tenant A cannot see tenant B's products and vice-versa."""
    prod_a = await _insert_product(adm, tenant_a, f"sku-a-{uuid.uuid4().hex[:6]}")
    prod_b = await _insert_product(adm, tenant_b, f"sku-b-{uuid.uuid4().hex[:6]}")

    try:
        # Tenant A context
        async with app.transaction():
            await _set_tenant(app, tenant_a)
            rows = await app.fetch(
                "SELECT id FROM products WHERE id = ANY($1::uuid[])", [prod_a, prod_b]
            )
            ids = {r["id"] for r in rows}
            assert ids == {prod_a}, f"Tenant A saw: {ids}"

        # Tenant B context
        async with app.transaction():
            await _set_tenant(app, tenant_b)
            rows = await app.fetch(
                "SELECT id FROM products WHERE id = ANY($1::uuid[])", [prod_a, prod_b]
            )
            ids = {r["id"] for r in rows}
            assert ids == {prod_b}, f"Tenant B saw: {ids}"
    finally:
        await adm.execute(
            "DELETE FROM products WHERE id = ANY($1::uuid[])", [prod_a, prod_b]
        )


# ── 2. No GUC set → no rows visible (fail closed) ────────────────────────────

async def test_no_tenant_context_blocks_all_reads(
    adm: asyncpg.Connection,
    app: asyncpg.Connection,
    tenant_a: uuid.UUID,
) -> None:
    """Without app.tenant_id set, the policy resolves to NULL — zero rows visible."""
    prod = await _insert_product(adm, tenant_a, f"sku-nc-{uuid.uuid4().hex[:6]}")

    try:
        async with app.transaction():
            # Deliberately do NOT set the GUC
            rows = await app.fetch("SELECT id FROM products WHERE id = $1", prod)
            assert rows == [], f"Expected no rows without GUC, got: {rows}"
    finally:
        await adm.execute("DELETE FROM products WHERE id = $1", prod)


# ── 3. INSERT with wrong tenant_id is rejected (WITH CHECK) ──────────────────

async def test_insert_wrong_tenant_id_rejected(
    app: asyncpg.Connection,
    tenant_a: uuid.UUID,
    tenant_b: uuid.UUID,
) -> None:
    """Inserting a row whose tenant_id differs from app.tenant_id raises an error."""
    await _set_tenant(app, tenant_a)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        # tenant_id = tenant_b while GUC = tenant_a  → WITH CHECK violation
        await app.execute(
            "INSERT INTO products (id, tenant_id, sku, name_uz, price_uzs)"
            " VALUES ($1, $2, 'bad-sku', 'Hacked', 0)",
            uuid.uuid4(), tenant_b,
        )
    # autocommit mode: failed statement leaves connection clean, no rollback needed


# ── 4. UPDATE cannot affect another tenant's rows (0 rows) ───────────────────

async def test_update_cannot_touch_other_tenant_rows(
    adm: asyncpg.Connection,
    app: asyncpg.Connection,
    tenant_a: uuid.UUID,
    tenant_b: uuid.UUID,
) -> None:
    """UPDATE targeting tenant B's rows while in tenant A context affects 0 rows."""
    prod_b = await _insert_product(adm, tenant_b, f"sku-upd-{uuid.uuid4().hex[:6]}", "Original")

    try:
        async with app.transaction():
            await _set_tenant(app, tenant_a)
            tag = await app.execute(
                "UPDATE products SET name_uz = 'Hijacked' WHERE id = $1", prod_b
            )
            assert tag == "UPDATE 0", f"Expected 0 rows updated, got: {tag}"

        # Verify the row was NOT modified
        original = await adm.fetchval(
            "SELECT name_uz FROM products WHERE id = $1", prod_b
        )
        assert original == "Original"
    finally:
        await adm.execute("DELETE FROM products WHERE id = $1", prod_b)


# ── 5. DELETE cannot affect another tenant's rows (0 rows) ───────────────────

async def test_delete_cannot_touch_other_tenant_rows(
    adm: asyncpg.Connection,
    app: asyncpg.Connection,
    tenant_a: uuid.UUID,
    tenant_b: uuid.UUID,
) -> None:
    """DELETE targeting tenant B's rows while in tenant A context affects 0 rows."""
    prod_b = await _insert_product(adm, tenant_b, f"sku-del-{uuid.uuid4().hex[:6]}")

    try:
        async with app.transaction():
            await _set_tenant(app, tenant_a)
            tag = await app.execute(
                "DELETE FROM products WHERE id = $1", prod_b
            )
            assert tag == "DELETE 0", f"Expected 0 rows deleted, got: {tag}"

        # Confirm the row still exists
        exists = await adm.fetchval(
            "SELECT 1 FROM products WHERE id = $1", prod_b
        )
        assert exists == 1
    finally:
        await adm.execute("DELETE FROM products WHERE id = $1", prod_b)


# ── 6. Normal INSERT/SELECT works when tenant_id matches GUC ─────────────────

async def test_own_tenant_insert_and_select_succeed(
    app: asyncpg.Connection,
    tenant_a: uuid.UUID,
    adm: asyncpg.Connection,
) -> None:
    """Happy path: inserting and reading own-tenant data works correctly."""
    pid = uuid.uuid4()
    try:
        async with app.transaction():
            await _set_tenant(app, tenant_a)
            await app.execute(
                "INSERT INTO products (id, tenant_id, sku, name_uz, price_uzs)"
                " VALUES ($1, $2, 'my-sku', 'Ko''rpa', 120000)",
                pid, tenant_a,
            )
            row = await app.fetchrow("SELECT * FROM products WHERE id = $1", pid)
            assert row is not None
            assert row["tenant_id"] == tenant_a
            assert row["name_uz"] == "Ko'rpa"
    finally:
        await adm.execute("DELETE FROM products WHERE id = $1", pid)


# ── 7. Isolation spans multiple tables ───────────────────────────────────────

async def test_multi_table_isolation(
    adm: asyncpg.Connection,
    app: asyncpg.Connection,
    tenant_a: uuid.UUID,
    tenant_b: uuid.UUID,
) -> None:
    """RLS is effective on agents and knowledge_docs too, not just products."""
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    doc_b = uuid.uuid4()

    await adm.execute(
        "INSERT INTO agents (id, tenant_id, type, name) VALUES ($1, $2, 'conversation', 'Bot A')",
        agent_a, tenant_a,
    )
    await adm.execute(
        "INSERT INTO agents (id, tenant_id, type, name) VALUES ($1, $2, 'content', 'Bot B')",
        agent_b, tenant_b,
    )
    await adm.execute(
        "INSERT INTO knowledge_docs (id, tenant_id, type, title, content, vector_id)"
        " VALUES ($1, $2, 'faq', 'FAQ B', 'Secret B data', 'vec-b')",
        doc_b, tenant_b,
    )

    try:
        async with app.transaction():
            await _set_tenant(app, tenant_a)

            agents = await app.fetch(
                "SELECT id FROM agents WHERE id = ANY($1::uuid[])", [agent_a, agent_b]
            )
            assert {r["id"] for r in agents} == {agent_a}

            docs = await app.fetch(
                "SELECT id FROM knowledge_docs WHERE id = $1", doc_b
            )
            assert docs == [], "Tenant A must not see tenant B knowledge docs"
    finally:
        await adm.execute(
            "DELETE FROM agents WHERE id = ANY($1::uuid[])", [agent_a, agent_b]
        )
        await adm.execute("DELETE FROM knowledge_docs WHERE id = $1", doc_b)
