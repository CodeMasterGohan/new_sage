"""Retrieval service for new_sage (spec §9, §10).

Local-first hybrid retrieval over versioned documentation:

1. Resolve (library_id, version) against the Postgres catalog (fail closed).
   Unresolvable version -> the ``is_latest`` version, flagged
   ``version_fallback=True`` in the response.
2. Build ONE :class:`VersionPredicate` and pass it IDENTICALLY to the
   dense, BM25, and identifier branches (§9.2).
3. Topic queries: embed query (EmbeddingGemma 2 task string sent to the
   embedding service, which applies the PrefixContract prompts server-side)
   -> dense + BM25 + identifier search -> RRF fusion -> MiniLM rerank
   (top 50, CPU) -> token-budget truncation.
4. No topic: overview mode — top chunks from getting-started/overview
   pages (fallback: first chunks), no embedding, no rerank.

All collaborators (embedding client, vector store, catalog, reranker) are
injectable interfaces — see :mod:`interfaces` — so unit tests run with
fakes and no Milvus, models, or Postgres.
"""

from .service import RetrievalService

__all__ = ["RetrievalService"]
