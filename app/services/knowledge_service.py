"""KnowledgeService — Qdrant-backed RAG with Voyage AI voyage-3 embeddings.

Collection naming: tenant_<uuid_no_hyphens>_knowledge
Separate collection per tenant keeps RLS-equivalent isolation at the vector layer.
"""

from __future__ import annotations

import logging
import uuid as _uuid_mod
from typing import Any

import httpx
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

from app.config import settings

logger = logging.getLogger(__name__)

_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
_VOYAGE_MODEL = "voyage-3"
_VECTOR_SIZE = 1024  # voyage-3 output dimension


def _collection_name(tenant_id: _uuid_mod.UUID) -> str:
    return f"tenant_{tenant_id.hex}_knowledge"


class KnowledgeService:
    def __init__(self, qdrant_client: AsyncQdrantClient) -> None:
        self._qdrant = qdrant_client

    # ── Embedding ─────────────────────────────────────────────────────────────

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        if not settings.voyage_api_key:
            raise RuntimeError("VOYAGE_API_KEY is not configured")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                _VOYAGE_URL,
                headers={"Authorization": f"Bearer {settings.voyage_api_key}"},
                json={"model": _VOYAGE_MODEL, "input": texts},
            )
            resp.raise_for_status()
            data = resp.json()
        return [item["embedding"] for item in data["data"]]

    # ── Collection management ─────────────────────────────────────────────────

    async def ensure_collection(self, tenant_id: _uuid_mod.UUID) -> None:
        name = _collection_name(tenant_id)
        existing = {c.name for c in (await self._qdrant.get_collections()).collections}
        if name not in existing:
            await self._qdrant.create_collection(
                collection_name=name,
                vectors_config=qmodels.VectorParams(
                    size=_VECTOR_SIZE,
                    distance=qmodels.Distance.COSINE,
                ),
            )

    # ── CRUD ──────────────────────────────────────────────────────────────────

    async def add_doc(
        self,
        *,
        tenant_id: _uuid_mod.UUID,
        doc_id: str,
        content: str,
        doc_type: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        await self.ensure_collection(tenant_id)
        vectors = await self._embed([content])
        point = qmodels.PointStruct(
            id=doc_id,
            vector=vectors[0],
            payload={
                "content": content,
                "type": doc_type,
                **(metadata or {}),
            },
        )
        await self._qdrant.upsert(
            collection_name=_collection_name(tenant_id),
            points=[point],
        )
        return doc_id

    async def search(
        self,
        *,
        tenant_id: _uuid_mod.UUID,
        query: str,
        top_k: int = 5,
        doc_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        await self.ensure_collection(tenant_id)
        vectors = await self._embed([query])
        query_filter = None
        if doc_types:
            query_filter = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="type",
                        match=qmodels.MatchAny(any=doc_types),
                    )
                ]
            )
        results = await self._qdrant.search(
            collection_name=_collection_name(tenant_id),
            query_vector=vectors[0],
            limit=top_k,
            query_filter=query_filter,
            with_payload=True,
        )
        return [
            {
                "id": str(r.id),
                "content": r.payload.get("content", ""),
                "type": r.payload.get("type", ""),
                "score": r.score,
                "metadata": {k: v for k, v in r.payload.items() if k not in ("content", "type")},
            }
            for r in results
        ]

    async def delete_doc(self, *, tenant_id: _uuid_mod.UUID, doc_id: str) -> None:
        await self._qdrant.delete(
            collection_name=_collection_name(tenant_id),
            points_selector=qmodels.PointIdsList(points=[doc_id]),
        )
