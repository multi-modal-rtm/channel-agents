from typing import Any


class TenantService:
    async def create_tenant(self, name: str, slug: str, plan: str = "starter") -> Any:
        raise NotImplementedError

    async def get_tenant_by_id(self, tenant_id: str) -> Any:
        raise NotImplementedError

    async def get_tenant_by_slug(self, slug: str) -> Any:
        raise NotImplementedError

    async def update_tenant(self, tenant_id: str, data: dict[str, Any]) -> Any:
        raise NotImplementedError

    async def set_byok_key(self, tenant_id: str, api_key: str) -> None:
        raise NotImplementedError
