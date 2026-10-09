"""Milvus ``sage_chunks`` vector store (spec §9).

Collection schema (dictated here; the deploy agent implements this same
schema in the milvus_setup service)::

    chunk_id    VARCHAR(64)            PK
    library_id  VARCHAR(64)
    version_id  VARCHAR(64)
    dense       FLOAT_VECTOR(768)
    sparse      SPARSE_FLOAT_VECTOR    (BM25 function attached by deploy)
    text        VARCHAR(65535)
    headings    VARCHAR(4096)          (JSON breadcrumb)
    url         VARCHAR(2048)
    token_count INT64
    ordinal     INT64                  (chunk order within page; Wave 2)

Branch notes:
- dense: ANN over ``dense`` (inner product; EmbeddingGemma 2 vectors are
  L2-normalized, so IP == cosine).
- bm25: full-text search over ``sparse`` — the raw query text is passed and
  the collection's server-side BM25 function analyzes it. Requires the
  deploy agent to have attached the BM25 function to ``sparse``; without it
  this branch fails closed with a DependencyError.
- identifier: ``query()`` with the version predicate ANDed with
  ``text``/``headings``/``url`` LIKE clauses per identifier token.
- overview: ``query()`` preferring headings that indicate
  getting-started/overview pages; falls back to the first chunks by
  ``ordinal`` ascending (deterministic since Wave 2 added the field).
"""

from __future__ import annotations

import json
import logging
from typing import Sequence

from .errors import DependencyError
from .interfaces import VectorStore
from .models import ScoredHit, StoredChunk
from .predicates import VersionPredicate, _escape

log = logging.getLogger(__name__)

COLLECTION = "sage_chunks"
OUTPUT_FIELDS = [
    "chunk_id",
    "library_id",
    "version_id",
    "text",
    "headings",
    "url",
    "token_count",
    "ordinal",
]

_OVERVIEW_TERMS = (
    "getting started",
    "getting-started",
    "quickstart",
    "quick start",
    "overview",
    "introduction",
)


def _like_escape(token: str) -> str:
    return _escape(token)  # same quote/backslash escaping as the predicate


class MilvusVectorStore(VectorStore):
    """pymilvus-backed :class:`VectorStore` for ``sage_chunks``."""

    def __init__(
        self,
        uri: str = "http://milvus:19530",
        collection_name: str = COLLECTION,
        token: str | None = None,
    ) -> None:
        from pymilvus import MilvusClient  # lazy: keeps unit tests import-light

        self._collection = collection_name
        self._client = MilvusClient(uri=uri, token=token) if token else MilvusClient(uri=uri)

    # ------------------------------------------------------------------
    # record mapping
    # ------------------------------------------------------------------
    @staticmethod
    def _to_record(row: dict) -> StoredChunk:
        headings = row.get("headings") or ""
        try:
            parsed = json.loads(headings) if isinstance(headings, str) else headings
            headings_t = tuple(str(h) for h in parsed) if isinstance(parsed, list) else ()
        except (ValueError, TypeError):
            headings_t = ()
        return StoredChunk(
            chunk_id=str(row.get("chunk_id", "")),
            library_id=str(row.get("library_id", "")),
            version_id=str(row.get("version_id", "")),
            text=str(row.get("text", "")),
            headings=headings_t,
            url=str(row.get("url", "")),
            token_count=int(row.get("token_count") or 0),
        )

    def _search(
        self,
        *,
        data: list,
        anns_field: str,
        search_params: dict,
        predicate: VersionPredicate,
        top_k: int,
    ) -> list[ScoredHit]:
        try:
            results = self._client.search(
                collection_name=self._collection,
                data=data,
                anns_field=anns_field,
                search_params=search_params,
                limit=top_k,
                filter=predicate.to_milvus_expr(),
                output_fields=OUTPUT_FIELDS,
            )
        except Exception as exc:
            raise DependencyError(
                f"milvus {anns_field} search failed: {exc}"
            ) from exc
        hits: list[ScoredHit] = []
        for rank, hit in enumerate(results[0] if results else [], start=1):
            entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
            record = self._to_record(entity)
            distance = hit.get("distance") if isinstance(hit, dict) else None
            hits.append(ScoredHit(record=record, rank=rank, score=distance))
        return hits

    # ------------------------------------------------------------------
    # VectorStore
    # ------------------------------------------------------------------
    def dense_search(
        self, vector: Sequence[float], predicate: VersionPredicate, top_k: int
    ) -> list[ScoredHit]:
        return self._search(
            data=[list(vector)],
            anns_field="dense",
            search_params={"metric_type": "IP", "params": {"nprobe": 16}},
            predicate=predicate,
            top_k=top_k,
        )

    def bm25_search(
        self, query_text: str, predicate: VersionPredicate, top_k: int
    ) -> list[ScoredHit]:
        return self._search(
            data=[query_text],
            anns_field="sparse",
            search_params={"metric_type": "BM25"},
            predicate=predicate,
            top_k=top_k,
        )

    def identifier_search(
        self, identifiers: Sequence[str], predicate: VersionPredicate, top_k: int
    ) -> list[ScoredHit]:
        tokens = [t for t in identifiers if t]
        if not tokens:
            return []
        clauses: list[str] = []
        for token in tokens:
            lit = _like_escape(f"%{token}%")
            clauses.append(
                f"(text like {lit} or headings like {lit} or url like {lit})"
            )
        expr = f"{predicate.to_milvus_expr()} and ({' or '.join(clauses)})"
        try:
            rows = self._client.query(
                collection_name=self._collection,
                filter=expr,
                output_fields=OUTPUT_FIELDS,
                limit=top_k,
            )
        except Exception as exc:
            raise DependencyError(f"milvus identifier query failed: {exc}") from exc
        return [
            ScoredHit(record=self._to_record(row), rank=i + 1)
            for i, row in enumerate(rows)
        ]

    def overview_chunks(
        self, predicate: VersionPredicate, top_k: int
    ) -> list[StoredChunk]:
        like_clauses = " or ".join(
            f"headings like {_like_escape(f'%{term}%')}" for term in _OVERVIEW_TERMS
        )
        expr = f"{predicate.to_milvus_expr()} and ({like_clauses})"
        try:
            rows = self._client.query(
                collection_name=self._collection,
                filter=expr,
                output_fields=OUTPUT_FIELDS,
                limit=top_k,
            )
            if not rows:
                # Fallback: predicate-only query, ordered by ordinal ascending
                # (Wave 2: the schema now carries ordinal INT64, so "first
                # chunks" is deterministic — page order, then chunk order).
                # pymilvus query() has no ORDER BY, so we sort client-side.
                rows = self._client.query(
                    collection_name=self._collection,
                    filter=predicate.to_milvus_expr(),
                    output_fields=OUTPUT_FIELDS,
                    limit=top_k,
                )
                rows = sorted(rows, key=lambda r: r.get("ordinal") or 0)
        except Exception as exc:
            raise DependencyError(f"milvus overview query failed: {exc}") from exc
        return [self._to_record(row) for row in rows]
