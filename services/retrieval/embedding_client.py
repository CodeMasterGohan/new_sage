"""HTTP client for the embedding service (spec §8).

Contract (the embedding agent owns the server side)::

    POST /embed  <- {"texts": [...], "task": "question_answering"}
                -> {"vectors": [[...]], "dimension": 768}

The client sends RAW text plus the task string. The embedding service
applies the contractual PrefixContract prompt strings server-side
(byte-identical to new_DRUID's ``services/ingestion/embeddings.py`` —
documented in this service's README, not duplicated here).

``POST /embed_documents`` exists on the same service for the ingestion
agent; retrieval never calls it.
"""

from __future__ import annotations

import logging

import httpx

from .errors import DependencyError
from .interfaces import EmbeddingClient
from .prompts import validate_task

log = logging.getLogger(__name__)


class HttpEmbeddingClient(EmbeddingClient):
    """Embedding client over HTTP. Fails closed on any contract violation."""

    def __init__(
        self,
        base_url: str = "http://embedding:8002",
        dimension: int = 768,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._dimension = dimension
        self._timeout = timeout

    def embed_query(self, text: str, task: str = "question_answering") -> list[float]:
        validate_task(task)
        try:
            response = httpx.post(
                f"{self._base_url}/embed",
                json={"texts": [text], "task": task},
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except DependencyError:
            raise
        except Exception as exc:
            raise DependencyError(f"embedding service request failed: {exc}") from exc
        vectors = payload.get("vectors")
        if not vectors:
            raise DependencyError("embedding service returned no vectors")
        vector = [float(x) for x in vectors[0]]
        if len(vector) != self._dimension:
            raise DependencyError(
                f"embedding width {len(vector)} != contract {self._dimension}; "
                "refusing to mix contracts (§8)"
            )
        return vector

    @property
    def dimension(self) -> int:
        return self._dimension
