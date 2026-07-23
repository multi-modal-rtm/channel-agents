"""Add customer unification tables.

Creates:
  - customers          (unified identity across IG + TG + phone)
  - customer_handoffs  (short-lived bridge tokens, 10-min TTL)

Alters:
  - conversations.customer_id  (nullable FK → customers.id)

Both new tables get RLS on the same pattern as the initial schema.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RLS_TABLES = ["customers", "customer_handoffs"]


def upgrade() -> None:
    op.execute("""
        CREATE TABLE customers (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            instagram_psid   TEXT,
            telegram_chat_id BIGINT,
            phone            TEXT,
            customer_name    TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_customers_tenant_id ON customers (tenant_id)")
    # Partial unique: one row per instagram_psid per tenant (ignores NULLs)
    op.execute(
        "CREATE UNIQUE INDEX uq_customers_tenant_instagram "
        "ON customers (tenant_id, instagram_psid) WHERE instagram_psid IS NOT NULL"
    )
    # Partial unique: one row per telegram_chat_id per tenant
    op.execute(
        "CREATE UNIQUE INDEX uq_customers_tenant_telegram "
        "ON customers (tenant_id, telegram_chat_id) WHERE telegram_chat_id IS NOT NULL"
    )

    op.execute("""
        CREATE TABLE customer_handoffs (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            customer_id  UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            channel_from TEXT NOT NULL,
            channel_to   TEXT NOT NULL,
            expires_at   TIMESTAMPTZ NOT NULL,
            redeemed_at  TIMESTAMPTZ,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX ix_customer_handoffs_tenant_id ON customer_handoffs (tenant_id)"
    )

    # Link conversations → customers (nullable; back-filled lazily)
    op.execute("ALTER TABLE conversations ADD COLUMN customer_id UUID REFERENCES customers(id)")
    op.execute(
        "CREATE INDEX ix_conversations_customer_id "
        "ON conversations (customer_id) WHERE customer_id IS NOT NULL"
    )

    # Row-Level Security — same pattern as initial schema
    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
                USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
        """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_conversations_customer_id")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS customer_id")

    for table in reversed(_RLS_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")

    op.execute("DROP TABLE IF EXISTS customer_handoffs CASCADE")
    op.execute("DROP TABLE IF EXISTS customers CASCADE")
