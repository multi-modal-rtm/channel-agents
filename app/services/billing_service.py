from typing import Any
from uuid import UUID


class BillingService:
    async def record_usage(
        self,
        tenant_id: UUID,
        model: str,
        input_tokens: int,
        output_tokens: int,
        agent_type: str | None = None,
        conversation_id: UUID | None = None,
    ) -> None:
        raise NotImplementedError

    async def get_tenant_usage(
        self,
        tenant_id: UUID,
        period_start: str | None = None,
        period_end: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError
