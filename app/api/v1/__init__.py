from fastapi import APIRouter

from app.api.v1 import agents, auth, conversations, integrations, products, tenants, webhooks

router = APIRouter(prefix="/api/v1")
router.include_router(auth.router)
router.include_router(tenants.router)
router.include_router(agents.router)
router.include_router(conversations.router)
router.include_router(products.router)
router.include_router(integrations.router)
router.include_router(webhooks.router)
