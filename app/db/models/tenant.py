import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, LargeBinary, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Tenant(Base, TimestampMixin):
    """Root table — NOT tenant-scoped, no RLS."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    plan: Mapped[str] = mapped_column(
        String(50), nullable=False, default="starter", server_default=text("'starter'")
    )
    billing_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default="managed", server_default=text("'managed'")
    )
    # Fernet-encrypted Anthropic key; NULL when billing_mode='managed'
    anthropic_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="trial", server_default=text("'trial'")
    )
    # Kill switch: pauses all autonomous agent actions for this tenant
    autonomous_actions_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # Optional hard cap on LLM spend per calendar day (UTC). NULL = unlimited.
    daily_budget_usd: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    # Telegram integration — one bot per tenant
    telegram_bot_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    telegram_webhook_secret: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # Instagram / Meta integration
    instagram_page_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    instagram_page_access_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    instagram_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    instagram_user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
