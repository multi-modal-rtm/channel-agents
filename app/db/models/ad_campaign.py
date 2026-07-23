import uuid
from datetime import datetime

from sqlalchemy import DateTime, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TenantScopedBase


class AdCampaign(Base, CreatedAtMixin, TenantScopedBase):
    __tablename__ = "ad_campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default=text("'active'")
    )
    daily_budget_uzs: Mapped[float | None] = mapped_column(Numeric(14, 2), nullable=True)
    total_spent_uzs: Mapped[float] = mapped_column(
        Numeric(14, 2), nullable=False, default=0, server_default=text("0")
    )
    performance_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'")
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
