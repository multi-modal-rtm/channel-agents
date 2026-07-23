import uuid

from sqlalchemy import JSON, Boolean, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class AgentConfig(Base, TimestampMixin, TenantMixin):
    __tablename__ = "agent_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # conversation | content | lead_qualifier
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(100), default="claude-haiku-4-5-20251001")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Agent-specific parameters (temperature, tools, etc.)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    system_prompt_override: Mapped[str | None] = mapped_column(Text, nullable=True)
