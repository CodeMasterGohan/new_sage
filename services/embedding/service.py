"""Task-aware embedding adapter with local CPU and shared GPU backends."""

from __future__ import annotations

import asyncio
import logging
import math
import threading

from . import reuse
from .settings import EmbeddingServiceSettings
from .vllm_backend import VllmEmbeddingBackend

log = logging.getLogger(__name__)

# Task names accepted by POST /embed, mapped to the contract's query tasks.
_TASK_TO_CONTRACT_TASK = {
    "question_answering": "qa",
    "code_retrieval": "code",
    "search_result": "search",
}


class EmbeddingService:
    """EmbeddingGemma 2 contract adapter."""

    def __init__(self, settings: EmbeddingServiceSettings | None = None) -> None:
        self.settings = settings or EmbeddingServiceSettings()
        self._ing = reuse.load_ingestion_embeddings()
        self._backend = self._build_backend()
        self._lock = threading.Lock()
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        self._verified = False

    # -- backend lifecycle -------------------------------------------------
    def _build_backend(self):
        ing = self._ing
        if self.settings.backend == "mock":
            log.warning("embedding backend=mock: deterministic fake vectors, NOT semantic")
            return ing.MockEmbeddingBackend(dimension=self.settings.dimension)
        if self.settings.backend == "vllm":
            return VllmEmbeddingBackend(self.settings)
        # Explicit local CPU option; pre-staged artifacts only.
        st_settings = ing.EmbeddingSettings(
            model_id=self.settings.model_id,
            modality_config=self.settings.modality_config,
            dimension=self.settings.dimension,
            normalize=self.settings.normalize,
        )
        return ing.SentenceTransformersGemmaBackend(
            settings=st_settings,
            model_path=self.settings.model_path or None,
        )

    @property
    def backend(self):
        return self._backend

    # -- §8 prefix contract (delegated to the vendored module; byte-identical)
    def format_document(self, *, title: str, text: str, is_code: bool = False) -> str:
        """Format a document-side input per the contractual prefix pair."""
        contract = self._ing.PrefixContract
        template = contract.DOC_CODE_TITLE if is_code else contract.DOC_PROSE_TITLE
        return template.format(heading=title, text=text)

    def format_query(self, *, text: str, task: str = "question_answering") -> str:
        """Format a query-side input per the contractual prefix pair."""
        contract_task = _TASK_TO_CONTRACT_TASK.get(task)
        if contract_task is None:
            raise ValueError(f"unknown task {task!r}")
        return self._ing.build_query_text(text, task=contract_task)

    # -- embedding ----------------------------------------------------------
    def _guard_text(self, text: str) -> None:
        if len(text) > self.settings.max_input_chars:
            raise ValueError(
                f"input too long ({len(text)} chars > "
                f"{self.settings.max_input_chars}): inputs must arrive "
                "pre-chunked (≤420 tokens, §7). Refusing to silently truncate."
            )

    def _verify_vector(self, vector: list[float]) -> None:
        """Fail-closed width + normalization check for every vector."""
        expected = self.settings.dimension
        if len(vector) != expected:
            raise RuntimeError(
                f"backend returned width {len(vector)}, expected {expected}: "
                "refusing to silently mix widths (§8 migration rule)."
            )
        if not all(math.isfinite(v) for v in vector):
            raise RuntimeError("backend returned non-finite embedding")
        if self.settings.normalize:
            norm = math.sqrt(sum(v * v for v in vector))
            if abs(norm - 1.0) > 1e-3:
                raise RuntimeError(
                    f"backend returned unnormalized vector (L2={norm:.4f}) "
                    "with normalize=true: fail closed."
                )
        self._verified = True

    def _encode_texts(self, formatted: list[str]) -> list[list[float]]:
        # sentence-transformers encode is run under a lock: CPU inference is
        # throughput-bound and the encoder is not guaranteed thread-safe.
        if self.settings.backend == "vllm":
            vectors = self._backend.embed_texts(formatted)
        else:
            with self._lock:
                vectors = self._backend.embed_texts(formatted)
        if len(vectors) != len(formatted):
            raise RuntimeError("backend returned wrong vector count")
        for v in vectors:
            self._verify_vector(v)
        return vectors

    async def embed_texts(self, formatted: list[str]) -> list[list[float]]:
        """Embed pre-formatted texts (prefixes already applied)."""
        if not formatted:
            raise ValueError("texts must be non-empty")
        for t in formatted:
            self._guard_text(t)
        out: list[list[float]] = []
        async with self._semaphore:
            for i in range(0, len(formatted), self.settings.batch_size):
                batch = formatted[i : i + self.settings.batch_size]
                out.extend(await asyncio.to_thread(self._encode_texts, batch))
        return out

    async def embed_documents(
        self,
        items: list[dict],
        *,
        task: str = "question_answering",
    ) -> list[list[float]]:
        """Embed document-side inputs, applying the §8 document prefix."""
        formatted = [
            self.format_document(
                title=item.get("title", ""),
                text=item["text"],
                is_code=(task == "code_retrieval") or bool(item.get("is_code")),
            )
            for item in items
        ]
        return await self.embed_texts(formatted)

    async def embed_queries(
        self, queries: list[str], *, task: str = "question_answering"
    ) -> list[list[float]]:
        """Embed query-side inputs, applying the §8 query prefix."""
        formatted = [self.format_query(text=q, task=task) for q in queries]
        return await self.embed_texts(formatted)

    def close(self) -> None:
        if self.settings.backend == "vllm":
            self._backend.close()

    def check_ready(self) -> None:
        if self.settings.backend == "vllm":
            self._backend.check_ready()

    def describe(self) -> dict:
        """Model/card info for GET /health."""
        return {
            "model": self.settings.model_id,
            "modality_config": self.settings.modality_config,
            "dimension": self.settings.dimension,
            "normalize": self.settings.normalize,
            "backend": self.settings.backend,
            "model_path": self.settings.model_path or None,
            "model_loaded": self._verified,
            "device": "cuda:0 (remote vLLM)" if self.settings.backend == "vllm" else "cpu",
        }
