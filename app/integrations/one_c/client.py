from typing import Any


class OneCClient:
    """1C:Enterprise HTTP service integration stub.

    Expected to sync product catalog and stock levels once implemented.
    """

    async def get_product_catalog(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def get_stock(self, sku: str) -> int:
        raise NotImplementedError

    async def create_order(self, order_data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
