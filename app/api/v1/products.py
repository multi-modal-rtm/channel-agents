from fastapi import APIRouter

router = APIRouter(prefix="/products", tags=["products"])


@router.get("/")
async def list_products() -> None:
    raise NotImplementedError


@router.post("/")
async def create_product() -> None:
    raise NotImplementedError


@router.get("/{product_id}")
async def get_product(product_id: str) -> None:
    raise NotImplementedError


@router.put("/{product_id}")
async def update_product(product_id: str) -> None:
    raise NotImplementedError


@router.delete("/{product_id}")
async def delete_product(product_id: str) -> None:
    raise NotImplementedError
