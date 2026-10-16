"""RetrievalService: the §9 search sequence.

``retrieve(library_id, version, topic, top_k, token_budget)`` is the
single-version entry point. ONE :class:`VersionPredicate` is built per call
and passed identically to the dense, BM25, and identifier branches (§9.2).
"""

from __future__ import annotations

import logging
import time

from .config import RetrievalConfig
from .errors import DependencyError, UnknownLibraryError, VersionResolutionError
from .fusion import apply_identifier_boost, reciprocal_rank_fusion
from .identifiers import extract_identifier_tokens, library_name_mentions
from .code_search import extract_code_symbols, code_match_strength
from .interfaces import (
    EmbeddingClient,
    LibraryCatalog,
    Reranker,
    VectorStore,
)
from .models import Passage, RetrieveResponse, StoredChunk, detect_code_language, estimate_tokens
from .predicates import VersionPredicate
from .prompts import detect_task
from .reranker import rerank_candidates

log = logging.getLogger(__name__)


def truncate_to_token_budget(
    passages: list[Passage], token_budget: int
) -> list[Passage]:
    """Drop passages past the token budget. Always keeps the first passage
    so the response is never empty when retrieval found something."""
    kept: list[Passage] = []
    used = 0
    for passage in passages:
        if kept and used + passage.token_count > token_budget:
            break
        kept.append(passage)
        used += passage.token_count
    return kept


class RetrievalService:
    """Version-scoped hybrid retrieval (§9). All collaborators are injected."""

    def __init__(
        self,
        *,
        embedding: EmbeddingClient,
        vector_store: VectorStore,
        catalog: LibraryCatalog,
        reranker: Reranker,
        config: RetrievalConfig | None = None,
    ) -> None:
        self._embedding = embedding
        self._store = vector_store
        self._catalog = catalog
        self._reranker = reranker
        self._config = config or RetrievalConfig()

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------
    def retrieve(
        self,
        library_id: str,
        version: str | None = None,
        topic: str | None = None,
        top_k: int = 50,
        token_budget: int = 10000,
        task_hint: str | None = None,
        diagnostics: bool = False,
    ) -> RetrieveResponse:
        """Execute the §9 retrieval sequence. Fails closed on bad input or
        any collaborator failure."""
        cfg = self._config
        top_k = max(1, min(top_k, cfg.max_top_k))
        token_budget = max(1, token_budget)
        log.info(
            "retrieve: library_id=%r version=%r topic=%r top_k=%d token_budget=%d",
            library_id,
            version,
            (topic[:60] + "…") if topic and len(topic) > 60 else topic,
            top_k,
            token_budget,
        )
        started = time.monotonic()

        # 1. Resolve the library and its version (fail closed).
        try:
            library = self._catalog.get_library(library_id)
        except Exception as exc:
            raise DependencyError(f"catalog library lookup failed: {exc}") from exc
        if library is None:
            log.warning("retrieve: unknown library_id %r", library_id)
            raise UnknownLibraryError(f"unknown library_id {library_id!r}")
        try:
            resolved = self._catalog.resolve_version(library_id, version)
        except VersionResolutionError:
            raise
        except DependencyError:
            raise
        except Exception as exc:
            raise DependencyError(f"version resolution failed: {exc}") from exc
        if resolved.fallback:
            log.warning(
                "retrieve: version_fallback=True for %r (requested %r) — serving %r",
                library_id,
                version,
                resolved.version,
            )

        # 2. ONE version predicate, applied identically to every branch.
        predicate = VersionPredicate(library_id=library_id, version_id=resolved.version_id)

        catalog_done = time.monotonic()
        library_info = {
            "library_id": library.library_id,
            "name": library.name,
            "ecosystem": library.ecosystem,
        }

        # 3. Overview mode: no topic -> no embedding, no rerank.
        if not (topic or "").strip():
            chunks = self._store.overview_chunks(
                predicate, min(top_k, cfg.overview_candidates)
            )
            passages = [self._to_passage(chunk, score=None) for chunk in chunks[:top_k]]
            passages = truncate_to_token_budget(passages, token_budget)
            log.info(
                "retrieve done (overview): %r@%r passages=%d elapsed=%.2fs",
                library_id,
                resolved.version,
                len(passages),
                time.monotonic() - started,
            )
            return RetrieveResponse(
                library=library_info,
                version=resolved.version,
                version_fallback=resolved.fallback,
                passages=passages,
                diagnostics=(
                    {"mode": "overview", "version_id": resolved.version_id,
                     "timings_ms": {"total": round((time.monotonic() - started) * 1000, 2)},
                     "returned_count": len(passages)} if diagnostics else None
                ),
            )

        # 4. Query embedding (task string sent; prompts applied server-side).
        task = detect_task(topic, task_hint)
        try:
            query_vector = self._embedding.embed_query(topic, task=task)
        except Exception as exc:
            raise DependencyError(f"query embedding failed: {exc}") from exc
        if (
            len(query_vector) != cfg.embedding_dimension
            or self._embedding.dimension != cfg.embedding_dimension
        ):
            raise DependencyError(
                f"embedding width {len(query_vector)} (client declares "
                f"{self._embedding.dimension}) != contract "
                f"{cfg.embedding_dimension}; refusing to mix contracts (§8)"
            )

        embedding_done = time.monotonic()
        # 5-7. Dense / BM25 / identifier retrieval — SAME predicate.
        try:
            dense_hits = self._store.dense_search(
                query_vector, predicate, cfg.dense_candidates
            )
            bm25_hits = self._store.bm25_search(
                topic, predicate, cfg.lexical_candidates
            )
            id_tokens = library_name_mentions(topic, library.name, library.aliases)
            id_tokens += [
                t for t in extract_identifier_tokens(topic) if t not in id_tokens
            ]
            id_tokens += [
                t for t in extract_code_symbols(topic)
                if t.casefold() not in {x.casefold() for x in id_tokens}
            ]
            identifier_hits = (
                self._store.identifier_search(
                    id_tokens, predicate, cfg.identifier_candidates
                )
                if id_tokens
                else []
            )
        except Exception as exc:
            raise DependencyError(f"vector store query failed: {exc}") from exc

        retrieval_done = time.monotonic()
        # 8. Deduplicate + RRF fusion; identifier hits merged with fixed boost.
        fused = reciprocal_rank_fusion(
            {"dense": dense_hits, "bm25": bm25_hits}, k=cfg.rrf_k
        )
        fused = apply_identifier_boost(fused, identifier_hits, cfg.identifier_boost)
        shortlist = fused[: cfg.rerank_candidates]

        # 9. MiniLM rerank over the top-50 shortlist (CPU).
        try:
            reranked = rerank_candidates(
                topic, shortlist, self._reranker,
                code_symbol_bonus=cfg.code_symbol_bonus,
            )
        except Exception as exc:
            raise DependencyError(f"rerank failed: {exc}") from exc
        rerank_done = time.monotonic()
        finalists = reranked[:top_k]

        # 10. Render passages and enforce the token budget.
        passages = [
            self._to_passage(candidate.record, score=score)
            for candidate, score in finalists
        ]
        passages = truncate_to_token_budget(passages, token_budget)

        log.info(
            "retrieve done: %r@%r passages=%d version_fallback=%s elapsed=%.2fs",
            library_id,
            resolved.version,
            len(passages),
            resolved.fallback,
            time.monotonic() - started,
        )
        trace = None
        if diagnostics:
            symbols = extract_code_symbols(topic)
            ranks = {
                branch: {hit.record.chunk_id: rank for rank, hit in enumerate(hits, 1)}
                for branch, hits in (
                    ("dense", dense_hits), ("bm25", bm25_hits),
                    ("identifier", identifier_hits),
                )
            }
            trace = {
                "mode": "hybrid",
                "version_id": resolved.version_id,
                "version_fallback": resolved.fallback,
                "symbols": symbols,
                "candidate_counts": {
                    "dense": len(dense_hits),
                    "bm25": len(bm25_hits),
                    "identifier": len(identifier_hits),
                    "fused": len(fused),
                    "reranked": len(reranked),
                    "returned": len(passages),
                },
                "timings_ms": {
                    "catalog": round((catalog_done - started) * 1000, 2),
                    "embedding": round((embedding_done - catalog_done) * 1000, 2),
                    "candidate_search": round((retrieval_done - embedding_done) * 1000, 2),
                    "reranking": round((rerank_done - retrieval_done) * 1000, 2),
                    "total": round((time.monotonic() - started) * 1000, 2),
                },
                "top_hits": [
                    {
                        "chunk_id": candidate.record.chunk_id,
                        "ranks": {
                            name: branch_ranks.get(candidate.record.chunk_id)
                            for name, branch_ranks in ranks.items()
                        },
                        "branches": list(candidate.branches),
                        "fusion_score": round(candidate.fusion_score, 6),
                        "rerank_score": round(score, 6),
                        "symbol_match": code_match_strength(symbols, candidate.record),
                    }
                    for candidate, score in reranked[: min(20, top_k)]
                ],
            }
        return RetrieveResponse(
            library=library_info,
            version=resolved.version,
            version_fallback=resolved.fallback,
            passages=passages,
            diagnostics=trace,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _to_passage(
        self, record: StoredChunk, score: float | None
    ) -> Passage:
        token_count = record.token_count or estimate_tokens(record.text)
        return Passage(
            text=record.text,
            url=record.url,
            section=list(record.headings),
            code_language=detect_code_language(record.text),
            score=score,
            token_count=token_count,
        )
