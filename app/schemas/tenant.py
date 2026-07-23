from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class TenantResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    name: str
    slug: str
    plan: str
    billing_mode: str
    status: str
    autonomous_actions_enabled: bool
    has_anthropic_key: bool
    created_at: datetime

    @classmethod
    def from_orm(cls, tenant) -> "TenantResponse":  # type: ignore[override]
        return cls(
            id=tenant.id,
            name=tenant.name,
            slug=tenant.slug,
            plan=tenant.plan,
            billing_mode=tenant.billing_mode,
            status=tenant.status,
            autonomous_actions_enabled=tenant.autonomous_actions_enabled,
            has_anthropic_key=tenant.anthropic_key_encrypted is not None,
            created_at=tenant.created_at,
        )


class TenantUpdateRequest(BaseModel):
    name: str | None = None
    plan: str | None = None


class AnthropicKeyRequest(BaseModel):
    api_key: str
