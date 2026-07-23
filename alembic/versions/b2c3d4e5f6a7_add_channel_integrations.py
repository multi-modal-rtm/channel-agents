"""Add Telegram + Instagram integration columns to tenants.

The initial schema created tenants with only core billing columns.
This migration adds per-channel fields that were added to the SQLAlchemy
model after the initial schema was written.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("daily_budget_usd", sa.Numeric(10, 2), nullable=True))
    op.add_column("tenants", sa.Column("telegram_bot_token_encrypted", sa.LargeBinary(), nullable=True))
    op.add_column("tenants", sa.Column("telegram_webhook_secret", sa.LargeBinary(), nullable=True))
    op.add_column("tenants", sa.Column("instagram_page_id", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("instagram_page_access_token_encrypted", sa.LargeBinary(), nullable=True))
    op.add_column("tenants", sa.Column("instagram_token_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tenants", sa.Column("instagram_user_id", sa.Text(), nullable=True))

    # Index to look up tenants by instagram_page_id at webhook time.
    op.create_index("ix_tenants_instagram_page_id", "tenants", ["instagram_page_id"])


def downgrade() -> None:
    op.drop_index("ix_tenants_instagram_page_id", "tenants")
    op.drop_column("tenants", "instagram_user_id")
    op.drop_column("tenants", "instagram_token_expires_at")
    op.drop_column("tenants", "instagram_page_access_token_encrypted")
    op.drop_column("tenants", "instagram_page_id")
    op.drop_column("tenants", "telegram_webhook_secret")
    op.drop_column("tenants", "telegram_bot_token_encrypted")
    op.drop_column("tenants", "daily_budget_usd")
