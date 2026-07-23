import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    @declared_attr
    def created_at(cls) -> Mapped[datetime]:  # noqa: N805
        return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    @declared_attr
    def updated_at(cls) -> Mapped[datetime]:  # noqa: N805
        return mapped_column(
            DateTime(timezone=True),
            server_default=func.now(),
            onupdate=func.now(),
            nullable=False,
        )


class CreatedAtMixin:
    """For append-only tables (messages, llm_calls, audit_log) that have no updated_at."""

    @declared_attr
    def created_at(cls) -> Mapped[datetime]:  # noqa: N805
        return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TenantScopedBase:
    """All tenant-scoped models inherit from this.

    Enforces tenant_id NOT NULL with an index whose first column is tenant_id.
    RLS policy in the DB uses current_setting('app.tenant_id') to filter rows.
    Never default or fabricate tenant_id — raise if context is missing (Rule 10).
    """

    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(UUID(as_uuid=True), nullable=False, index=True)


# Backwards-compat alias — use TenantScopedBase in new code.
TenantMixin = TenantScopedBase
