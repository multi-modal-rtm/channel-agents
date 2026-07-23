"""Unified customer identity across Instagram and Telegram channels.

One Customer row represents one real person, regardless of which channel
they first contacted on. Channels are linked by updating the corresponding
field when the identity is confirmed (DM on IG, /start on TG, phone number).

CustomerHandoff is a short-lived token that bridges an Instagram customer
to a Telegram conversation. The agent generates it when calling
suggest_telegram_handoff; the Telegram /start handler redeems it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TenantScopedBase, TimestampMixin


class Customer(Base, TimestampMixin, TenantScopedBase):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Channel-specific identifiers — any can be None until confirmed
    instagram_psid: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_name: Mapped[str | None] = mapped_column(Text, nullable=True)


class CustomerHandoff(Base, CreatedAtMixin, TenantScopedBase):
    """Short-lived bridge token from Instagram → Telegram.

    id doubles as the token embedded in the Telegram deep link.
    Expires 10 minutes after creation; single-use (redeemed_at is set on use).
    """

    __tablename__ = "customer_handoffs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_from: Mapped[str] = mapped_column(Text, nullable=False)
    channel_to: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redeemed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
