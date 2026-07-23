from contextlib import asynccontextmanager
import time

import structlog
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import router as v1_router
from app.config import settings
from app.core.logging import configure_logging
from app.core.metrics import (
    CONTENT_TYPE_LATEST,
    generate_latest,
    http_request_duration_seconds,
    http_requests_total,
)

configure_logging(settings.environment)
logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("startup", app=settings.app_name, environment=settings.environment)
    app.state.arq_redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    yield
    await app.state.arq_redis.close()
    logger.info("shutdown")


app = FastAPI(
    title="Channel Agents API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.debug else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(v1_router)


# ── Prometheus instrumentation middleware ─────────────────────────────────────

@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    # Normalise path to avoid cardinality explosion from UUID path segments
    path = _normalise_path(request.url.path)
    method = request.method
    t0 = time.monotonic()
    response = await call_next(request)
    latency = time.monotonic() - t0
    http_requests_total.labels(
        method=method,
        endpoint=path,
        status_code=str(response.status_code),
    ).inc()
    http_request_duration_seconds.labels(method=method, endpoint=path).observe(latency)
    return response


def _normalise_path(path: str) -> str:
    """Replace UUIDs in paths with {id} to keep label cardinality low."""
    import re
    return re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "{id}",
        path,
        flags=re.IGNORECASE,
    )


# ── Health + Metrics ──────────────────────────────────────────────────────────

@app.get("/health", tags=["health"])
async def health() -> dict:
    return {"status": "ok", "environment": settings.environment}


@app.get("/metrics", tags=["observability"], include_in_schema=False)
async def metrics() -> Response:
    """Prometheus scrape endpoint. Protect with network policy in production."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
