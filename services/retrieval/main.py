"""Retrieval service HTTP entrypoint (port 8001).

``POST /retrieve`` implements the MCP server's retrieval backend contract.
No authentication (spec §13: binds to localhost/Tailscale only; the
compose stack owns the bind address).
"""

from __future__ import annotations

import logging
import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .catalog import PostgresLibraryCatalog
from .config import RetrievalConfig
from .embedding_client import HttpEmbeddingClient
from .errors import DependencyError, RetrievalError, UnknownLibraryError
from .milvus_store import MilvusVectorStore
from .reranker import MiniLMReranker
from .service import RetrievalService

log = logging.getLogger("retrieval")

app = FastAPI(title="new_sage retrieval service")

_service: RetrievalService | None = None


class RetrieveRequest(BaseModel):
    library_id: str = Field(..., min_length=1)
    version: str | None = None
    topic: str | None = None
    top_k: int = Field(default=50, ge=1)
    token_budget: int = Field(default=10000, ge=1)


def build_service() -> RetrievalService:
    """Wire the production collaborators from the environment. Fails closed
    (raises) when required configuration or artifacts are missing."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return RetrievalService(
        embedding=HttpEmbeddingClient(
            base_url=os.environ.get("EMBEDDING_URL", "http://embedding:8002"),
        ),
        vector_store=MilvusVectorStore(
            uri=os.environ.get("MILVUS_URI", "http://milvus:19530"),
            collection_name=os.environ.get("MILVUS_COLLECTION", "sage_chunks"),
            token=os.environ.get("MILVUS_TOKEN") or None,
        ),
        catalog=PostgresLibraryCatalog(database_url),
        reranker=MiniLMReranker(),  # raises when MINILM_MODEL_PATH is missing
        config=RetrievalConfig.from_env(),
    )


def wire(service: RetrievalService) -> None:
    """Inject a pre-built RetrievalService (done at startup; also used by
    tests to bypass production wiring)."""
    global _service
    _service = service


def get_service() -> RetrievalService:
    if _service is None:
        raise HTTPException(status_code=503, detail="retrieval service not wired")
    return _service


@app.on_event("startup")
def _startup() -> None:
    wire(build_service())
    cfg = RetrievalConfig.from_env()
    log.info(
        "new_sage retrieval service starting: port=8001 "
        "milvus_uri=%s collection=%s embedding_url=%s device=cpu "
        "embed_dim=%d rerank_candidates=%d rrf_k=%d (no auth — spec §13)",
        os.environ.get("MILVUS_URI", "http://milvus:19530"),
        os.environ.get("MILVUS_COLLECTION", "sage_chunks"),
        os.environ.get("EMBEDDING_URL", "http://embedding:8002"),
        cfg.embedding_dimension,
        cfg.rerank_candidates,
        cfg.rrf_k,
    )
    log.info("retrieval service wired")


@app.post("/retrieve")
async def retrieve(body: RetrieveRequest):
    log.info(
        "retrieve: library_id=%r version=%r topic=%r top_k=%d token_budget=%d",
        body.library_id,
        body.version,
        (body.topic[:60] + "…") if body.topic and len(body.topic) > 60 else body.topic,
        body.top_k,
        body.token_budget,
    )
    started = time.monotonic()
    try:
        response = get_service().retrieve(
            library_id=body.library_id,
            version=body.version,
            topic=body.topic,
            top_k=body.top_k,
            token_budget=body.token_budget,
        )
    except UnknownLibraryError as exc:
        log.warning("retrieve: unknown library_id %r", body.library_id)
        raise HTTPException(status_code=404, detail=str(exc))
    except DependencyError as exc:
        log.warning("retrieval dependency failure: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    except RetrievalError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    log.info(
        "retrieve done: library_id=%r version=%r version_fallback=%s passages=%d elapsed=%.2fs",
        body.library_id,
        response.version,
        response.version_fallback,
        len(response.passages),
        time.monotonic() - started,
    )
    return response.to_dict()


@app.get("/health")
async def health():
    return {"status": "ok", "wired": _service is not None}


@app.exception_handler(Exception)
async def _unhandled(request, exc):  # noqa: ANN001, ANN202, ARG001
    log.exception("unhandled retrieval error")
    return JSONResponse(status_code=500, detail="internal retrieval error")
