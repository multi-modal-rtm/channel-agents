from typing import Any
from uuid import UUID


class ProductService:
    async def list_products(
        self, tenant_id: UUID, category: str | None = None, active_only: bool = True
    ) -> list[Any]:
        raise NotImplementedError

    async def create_product(self, tenant_id: UUID, data: dict[str, Any]) -> Any:
        raise NotImplementedError

    async def get_product(self, product_id: UUID) -> Any:
        raise NotImplementedError

    async def update_product(self, product_id: UUID, data: dict[str, Any]) -> Any:
        raise NotImplementedError

    async def delete_product(self, product_id: UUID) -> None:
        raise NotImplementedError
