"""Embedding API with explicit CPU or shared GPU inference."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from enum import Enum

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .service import EmbeddingService
from .settings import EmbeddingServiceSettings

log = logging.getLogger(__name__)

service: EmbeddingService | None = None


class Task(str, Enum):
    question_answering = "question_answering"
    code_retrieval = "code_retrieval"
    search_result = "search_result"


class EmbedRequest(BaseModel):
    # Query-side inputs: the §8 QUERY prefix pair is applied server-side,
    # selected by `task` (contractual — never client-supplied).
    texts: list[str] = Field(min_length=1)
    task: Task = Field(default=Task.question_answering)


class DocumentInput(BaseModel):
    title: str = Field(default="")
    text: str = Field(min_length=1)
    is_code: bool = Field(default=False)


class EmbedDocumentsRequest(BaseModel):
    # Document-side inputs: the §8 DOC prefix pair is applied server-side.
    items: list[DocumentInput] = Field(min_length=1)
    task: Task = Field(default=Task.question_answering)


class VectorsResponse(BaseModel):
    vectors: list[list[float]]
    dimension: int


@asynccontextmanager
async def lifespan(app: FastAPI):
    global service
    settings = EmbeddingServiceSettings()
    log.info(
        "starting embedding service: model=%s modality=%s dim=%d backend=%s port=%d",
        settings.model_id,
        settings.modality_config,
        settings.dimension,
        settings.backend,
        settings.port,
    )
    try:
        service = EmbeddingService(settings)
        # Warmup forces the backend to load NOW: missing/invalid artifacts
        # fail startup loudly instead of on first request (spec §14).
        await service.embed_texts(["sage warmup"])
    except Exception:
        if service is not None:
            service.close()
            service = None
        log.critical("embedding service failed startup — refusing to run degraded", exc_info=True)
        raise
    log.info("embedding service ready")
    try:
        yield
    finally:
        service.close()
        service = None


app = FastAPI(title="new_sage embedding service", version="1.0.0", lifespan=lifespan)


def _svc() -> EmbeddingService:
    if service is None:
        raise HTTPException(status_code=503, detail="embedding backend not ready")
    return service


@app.get("/health")
async def health() -> dict:
    if service is None:
        raise HTTPException(status_code=503, detail="embedding backend not ready")
    try:
        await asyncio.to_thread(service.check_ready)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="shared embedding backend unavailable") from exc
    info = service.describe()
    return {
        "model": info["model"],
        "dimension": info["dimension"],
        "backend": info["backend"],
        "device": info["device"],
        "model_loaded": info["model_loaded"],
    }


@app.post("/embed", response_model=VectorsResponse)
async def embed(req: EmbedRequest) -> VectorsResponse:
    """Embed query-side texts; the §8 query prefix is applied server-side."""
    svc = _svc()
    try:
        vectors = await svc.embed_queries(req.texts, task=req.task.value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        log.error("embed failed for %d text(s): %s", len(req.texts), exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return VectorsResponse(vectors=vectors, dimension=svc.settings.dimension)


@app.post("/embed_documents", response_model=VectorsResponse)
async def embed_documents(req: EmbedDocumentsRequest) -> VectorsResponse:
    """Embed document-side items; the §8 document prefix is applied server-side."""
    svc = _svc()
    try:
        vectors = await svc.embed_documents(
            [d.model_dump() for d in req.items],
            task=req.task.value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        log.error("embed_documents failed for %d item(s): %s", len(req.items), exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return VectorsResponse(vectors=vectors, dimension=svc.settings.dimension)
