from typing import Any


class Bitrix24Client:
    """Bitrix24 CRM integration stub."""

    async def create_lead(self, lead_data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def update_deal(self, deal_id: int, data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def add_comment(self, entity_id: int, text: str) -> dict[str, Any]:
        raise NotImplementedError
