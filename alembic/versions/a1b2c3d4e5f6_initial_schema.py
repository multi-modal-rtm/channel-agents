"""Initial schema with RLS policies.

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-05-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every tenant-scoped table gets identical RLS treatment.
TENANT_SCOPED_TABLES = [
    "users",
    "agents",
    "conversations",
    "messages",
    "products",
    "knowledge_docs",
    "ad_campaigns",
    "llm_calls",
    "audit_log",
]


def upgrade() -> None:
    # ── Root table (no RLS) ────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE tenants (
            id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name                      TEXT NOT NULL,
            slug                      TEXT NOT NULL,
            plan                      TEXT NOT NULL DEFAULT 'starter',
            billing_mode              TEXT NOT NULL DEFAULT 'managed',
            anthropic_key_encrypted   BYTEA,
            status                    TEXT NOT NULL DEFAULT 'trial',
            autonomous_actions_enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE UNIQUE INDEX ix_tenants_slug ON tenants (slug)")

    # ── Tenant-scoped tables (tenant_id is always the second column) ───────────
    op.execute("""
        CREATE TABLE users (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     UUID NOT NULL REFERENCES tenants(id),
            email         TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'agent',
            full_name     TEXT,
            is_active     BOOLEAN NOT NULL DEFAULT TRUE,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_users_tenant_id ON users (tenant_id)")
    op.execute("CREATE UNIQUE INDEX uq_users_tenant_email ON users (tenant_id, email)")

    op.execute("""
        CREATE TABLE agents (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       UUID NOT NULL REFERENCES tenants(id),
            type            TEXT NOT NULL,
            name            TEXT NOT NULL,
            system_prompt   TEXT,
            config_json     JSONB NOT NULL DEFAULT '{}',
            enabled         BOOLEAN NOT NULL DEFAULT TRUE,
            autonomy_level  INTEGER NOT NULL DEFAULT 0,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_agents_tenant_id ON agents (tenant_id)")

    op.execute("""
        CREATE TABLE conversations (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        UUID NOT NULL REFERENCES tenants(id),
            channel          TEXT NOT NULL,
            customer_handle  TEXT NOT NULL,
            customer_name    TEXT,
            customer_phone   TEXT,
            status           TEXT NOT NULL DEFAULT 'active',
            assigned_user_id UUID REFERENCES users(id),
            last_message_at  TIMESTAMPTZ,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_conversations_tenant_id ON conversations (tenant_id)")
    # Composite unique enables composite FK from messages (tenant_id, conversation_id)
    op.execute("CREATE UNIQUE INDEX uq_conversations_tenant_id ON conversations (tenant_id, id)")

    op.execute("""
        CREATE TABLE messages (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       UUID NOT NULL REFERENCES tenants(id),
            conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL,
            agent_id        UUID REFERENCES agents(id),
            human_user_id   UUID REFERENCES users(id),
            cost_usd        NUMERIC(10, 6),
            tokens_in       INTEGER,
            tokens_out      INTEGER,
            metadata        JSONB NOT NULL DEFAULT '{}',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_messages_tenant_id ON messages (tenant_id)")
    op.execute("CREATE INDEX ix_messages_conversation_id ON messages (tenant_id, conversation_id)")

    op.execute("""
        CREATE TABLE products (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   UUID NOT NULL REFERENCES tenants(id),
            sku         TEXT NOT NULL,
            name_uz     TEXT NOT NULL,
            name_ru     TEXT,
            description TEXT,
            price_uzs   NUMERIC(14, 2) NOT NULL DEFAULT 0,
            in_stock    BOOLEAN NOT NULL DEFAULT TRUE,
            photos      JSONB NOT NULL DEFAULT '[]',
            vector_id   TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_products_tenant_id ON products (tenant_id)")
    op.execute("CREATE UNIQUE INDEX uq_products_tenant_sku ON products (tenant_id, sku)")

    op.execute("""
        CREATE TABLE knowledge_docs (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id  UUID NOT NULL REFERENCES tenants(id),
            type       TEXT NOT NULL,
            title      TEXT NOT NULL,
            content    TEXT NOT NULL,
            vector_id  TEXT NOT NULL,
            source_url TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_knowledge_docs_tenant_id ON knowledge_docs (tenant_id)")

    op.execute("""
        CREATE TABLE ad_campaigns (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         UUID NOT NULL REFERENCES tenants(id),
            platform          TEXT NOT NULL,
            external_id       TEXT NOT NULL,
            name              TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'active',
            daily_budget_uzs  NUMERIC(14, 2),
            total_spent_uzs   NUMERIC(14, 2) NOT NULL DEFAULT 0,
            performance_json  JSONB NOT NULL DEFAULT '{}',
            last_synced_at    TIMESTAMPTZ,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_ad_campaigns_tenant_id ON ad_campaigns (tenant_id)")

    op.execute("""
        CREATE TABLE llm_calls (
            id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id  UUID NOT NULL REFERENCES tenants(id),
            agent_id   UUID REFERENCES agents(id),
            model      TEXT NOT NULL,
            tokens_in  INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            cost_usd   NUMERIC(10, 6) NOT NULL DEFAULT 0,
            latency_ms INTEGER,
            success    BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_llm_calls_tenant_id ON llm_calls (tenant_id)")
    op.execute("CREATE INDEX ix_llm_calls_tenant_created ON llm_calls (tenant_id, created_at DESC)")

    op.execute("""
        CREATE TABLE audit_log (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id    UUID NOT NULL REFERENCES tenants(id),
            user_id      UUID,
            agent_id     UUID REFERENCES agents(id),
            action       TEXT NOT NULL,
            entity_type  TEXT NOT NULL,
            entity_id    UUID NOT NULL,
            payload_json JSONB NOT NULL DEFAULT '{}',
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX ix_audit_log_tenant_id ON audit_log (tenant_id)")
    op.execute("CREATE INDEX ix_audit_log_tenant_created ON audit_log (tenant_id, created_at DESC)")

    # ── Row-Level Security ─────────────────────────────────────────────────────
    # Policy uses nullif so an unset GUC returns NULL → no rows visible (fail closed).
    # FORCE RLS makes the policy apply to the table owner too (defence-in-depth).
    for table in TENANT_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
                USING      (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
        """)


def downgrade() -> None:
    # Drop in reverse FK order
    for table in reversed(TENANT_SCOPED_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP TABLE IF EXISTS tenants CASCADE")
