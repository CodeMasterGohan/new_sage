"""Eval harness for new_sage (spec §16).

Drives the REAL components wherever possible without model weights:

- library resolution: the real ``LibraryResolver`` (mcp_server) over the
  hand-written seed table.
- retrieval: the real ``RetrievalService`` (retrieval) with injected fakes:
  a zero-vector embedding client, a BM25-backed fake vector store
  (``--branch bm25``), a no-op reranker, and a seed-backed fake catalog.
- chunking: the real ``DocChunker`` (ingestion) with a deterministic
  word-count ``token_len``.

Two branches:

- ``--branch bm25``: runs NOW on this VM. Dense search returns no hits
  (no EmbeddingGemma 2 weights — downloading is forbidden), so the
  measured recall@k is a BM25-only FLOOR, not the headline number.
- ``--branch hybrid``: dense + MiniLM rerank. STUBBED — exits with a clear
  BLOCKED message (exit code 2) when the pre-staged model artifacts are
  absent. This is the branch that runs on the H200.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("new_sage.eval")

EVAL_DIR = Path(__file__).resolve().parent
SERVICES_DIR = EVAL_DIR.parent  # build/services
MCP_SRC = SERVICES_DIR / "mcp_server" / "src"
sys.path.insert(0, str(SERVICES_DIR))
sys.path.insert(0, str(MCP_SRC))

from mcp_server.resolver import LibraryResolver  # noqa: E402
from ingestion.doc_chunker import ChunkerSettings, DocChunker  # noqa: E402
from retrieval.service import RetrievalService  # noqa: E402
from retrieval.config import RetrievalConfig  # noqa: E402
from retrieval.interfaces import (  # noqa: E402
    EmbeddingClient,
    LibraryCatalog,
    LibraryRecord as CatalogLibraryRecord,
    ResolvedVersion,
    Reranker,
    VectorStore,
)
from retrieval.models import StoredChunk  # noqa: E402
from retrieval.predicates import VersionPredicate  # noqa: E402

from corpus import (  # noqa: E402
    CORPUS_FILE,
    DOCS_DIR,
    RESOLVER_CASES_FILE,
    EvalDoc,
    EvalItem,
    load_corpus,
    load_docs,
    load_resolver_cases,
)
from metrics import citation_accuracy, mean, ndcg_at_k, recall_at_k, reciprocal_rank, resolver_accuracy, resolver_candidate_accuracy, version_correctness  # noqa: E402

# ---------------------------------------------------------------------------
# deterministic tokenizer stand-in (same contract as ingestion's test helper)
# ---------------------------------------------------------------------------


def word_len(text: str) -> int:
    """Deterministic stand-in for a tokenizer (1 word ~= 1 token).

    Production wires the pre-staged MiniLM tokenizer; the word count is a
    deliberate over-approximation for prose and keeps the eval fully offline.
    """
    return len(text.split())


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


# ---------------------------------------------------------------------------
# tiny real BM25 ranker (no models, no deps)
# ---------------------------------------------------------------------------


class BM25Index:
    """BM25 over a fixed set of documents. k1=1.2, b=0.75, standard idf."""

    def __init__(self, docs: dict[str, str], k1: float = 1.2, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._doc_tokens: dict[str, list[str]] = {}
        self._df: dict[str, int] = {}
        total_len = 0
        for doc_id, text in docs.items():
            toks = _tokens(text)
            self._doc_tokens[doc_id] = toks
            total_len += len(toks)
            for term in set(toks):
                self._df[term] = self._df.get(term, 0) + 1
        self._n = len(docs)
        self._avgdl = total_len / self._n if self._n else 0.0

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        return math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))

    def score(self, doc_id: str, query_terms: list[str]) -> float:
        toks = self._doc_tokens[doc_id]
        if not toks:
            return 0.0
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks)
        total = 0.0
        for term in set(query_terms):
            f = tf.get(term, 0)
            if not f:
                continue
            denom = f + self._k1 * (1 - self._b + self._b * dl / self._avgdl)
            total += self._idf(term) * f * (self._k1 + 1) / denom
        return total

    def search(
        self, query: str, candidate_ids: list[str], top_n: int
    ) -> list[tuple[str, float]]:
        qterms = _tokens(query)
        scored = [(cid, self.score(cid, qterms)) for cid in candidate_ids]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_n]


# ---------------------------------------------------------------------------
# injected fakes (BM25 branch)
# ---------------------------------------------------------------------------


class FakeCatalog(LibraryCatalog):
    """Seed-table-backed catalog: exact version match, else latest+fallback."""

    def __init__(self, seed_records: list) -> None:
        # seed_records are mcp_server LibraryRecord rows (pydantic).
        self._by_id = {r.library_id: r for r in seed_records}

    def get_library(self, library_id: str) -> CatalogLibraryRecord | None:
        rec = self._by_id.get(library_id)
        if rec is None:
            return None
        return CatalogLibraryRecord(
            library_id=rec.library_id,
            name=rec.name,
            ecosystem=rec.ecosystem,
            aliases=tuple(rec.aliases),
        )

    def resolve_version(
        self, library_id: str, version: str | None
    ) -> ResolvedVersion:
        rec = self._by_id[library_id]
        if version and version in rec.versions:
            return ResolvedVersion(version_id=version, version=version, fallback=False)
        latest = rec.latest
        return ResolvedVersion(version_id=latest, version=latest, fallback=True)


class FakeEmbedding(EmbeddingClient):
    """Zero-vector stand-in: dense branch contributes nothing (BM25 floor)."""

    dimension = 768

    def embed_query(self, text: str, task: str = "question_answering") -> list[float]:
        return [0.0] * self.dimension


class FakeReranker(Reranker):
    """No-op reranker: equal scores keep the RRF-fused order (stable sort)."""

    def score(self, query: str, passages) -> list[float]:
        return [0.0] * len(passages)


class FakeVectorStore(VectorStore):
    """BM25-backed store. Records every predicate it receives (§9.2 audit)."""

    def __init__(self, chunks: list[StoredChunk], index: BM25Index) -> None:
        self._chunks = {c.chunk_id: c for c in chunks}
        self._index = index
        self.predicates_seen: list[VersionPredicate] = []

    def _scoped(self, predicate: VersionPredicate) -> list[StoredChunk]:
        self.predicates_seen.append(predicate)
        return [
            c
            for c in self._chunks.values()
            if c.library_id == predicate.library_id
            and c.version_id == predicate.version_id
        ]

    def dense_search(self, query_vector, predicate, top_n) -> list:
        self.predicates_seen.append(predicate)
        return []  # no dense weights on this VM: BM25-only floor

    def bm25_search(self, query, predicate, top_n) -> list:
        from retrieval.models import ScoredHit

        scoped = self._scoped(predicate)
        ranked = self._index.search(query, [c.chunk_id for c in scoped], top_n)
        out = []
        for rank, (cid, score) in enumerate(ranked, start=1):
            out.append(ScoredHit(record=self._chunks[cid], rank=rank, score=score))
        return out

    def identifier_search(self, tokens, predicate, top_n) -> list:
        from retrieval.models import ScoredHit

        scoped = self._scoped(predicate)
        scored = []
        for chunk in scoped:
            hay = (
                " ".join([chunk.text, *chunk.headings]).lower()
            )
            hits = sum(1 for t in tokens if t.lower() in hay)
            if hits:
                scored.append((hits, chunk))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            ScoredHit(record=chunk, rank=rank, score=float(hits))
            for rank, (hits, chunk) in enumerate(scored[:top_n], start=1)
        ]

    def overview_chunks(self, predicate, top_n) -> list:
        self.predicates_seen.append(predicate)
        return []


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------

SPEC_CHUNK_BUDGET = 420  # spec §7 / §17.4 target; chunker hard budget is 512


@dataclass
class EvalHarness:
    items: list[EvalItem]
    docs: list[EvalDoc]
    resolver: LibraryResolver
    seed_records: list = field(default_factory=list)

    chunks: list = field(default_factory=list, init=False)
    stored: list[StoredChunk] = field(default_factory=list, init=False)
    gold_chunk_id: dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        chunker = DocChunker(ChunkerSettings(), token_len=word_len)
        for doc in self.docs:
            page_chunks = chunker.chunk_page(
                library_id=doc.library,
                version=doc.version,
                page_url=doc.url,
                page_hash=doc.page_hash,
                markdown=doc.markdown,
            )
            self.chunks.extend(page_chunks)
            for ch in page_chunks:
                self.stored.append(
                    StoredChunk(
                        chunk_id=ch.chunk_id,
                        library_id=ch.library_id,
                        version_id=ch.version_id,
                        text=ch.text,
                        headings=tuple(ch.headings),
                        url=ch.url,
                        token_count=ch.token_count,
                    )
                )
        # map each item to the chunk containing its gold passage (verbatim)
        for item in self.items:
            matches = [
                ch
                for ch in self.chunks
                if ch.library_id == item.library
                and ch.version_id == item.version
                and item.gold_passage_text in ch.text
            ]
            if not matches:
                raise ValueError(
                    f"gold passage for {item.id} not found verbatim in "
                    f"{item.library}__{item.version}.md"
                )
            # first match wins; passages are unique to their section chunk
            self.gold_chunk_id[item.id] = matches[0].chunk_id

    # -- chunking calibration (§7) ------------------------------------------
    def chunk_calibration(self) -> dict:
        violations: list[dict] = []
        code_broken = 0
        max_tokens = 0
        for ch in self.chunks:
            max_tokens = max(max_tokens, ch.token_count)
            if ch.token_count > SPEC_CHUNK_BUDGET:
                violations.append(
                    {
                        "chunk_id": ch.chunk_id,
                        "library": ch.library_id,
                        "version": ch.version_id,
                        "token_count": ch.token_count,
                    }
                )
            if ch.text.count("```") % 2 != 0:
                code_broken += 1
        return {
            "n_chunks": len(self.chunks),
            "max_tokens": max_tokens,
            "spec_budget": SPEC_CHUNK_BUDGET,
            "budget_violations": violations,
            "budget_compliant": len(violations) == 0,
            "code_blocks_intact": code_broken == 0,
            "code_blocks_broken": code_broken,
            "oversized_code_chunks": sum(
                1 for ch in self.chunks if ch.oversized_code
            ),
        }

    # -- resolver eval (real LibraryResolver) --------------------------------
    def run_resolver_eval(self, cases: list[dict]) -> dict:
        return resolver_accuracy(cases, self.resolver)

    # -- BM25 branch (runs NOW) ----------------------------------------------
    def _bm25_text(self, chunk: StoredChunk) -> str:
        # §9.1: headings carry BM25 weight; identifiers boost API lookups.
        return " ".join(
            [chunk.text, " ".join(chunk.headings), " ".join(chunk.headings)]
        )

    def run_bm25(self, ks: tuple[int, ...] = (1, 3, 5, 10)) -> dict:
        index = BM25Index({c.chunk_id: self._bm25_text(c) for c in self.stored})
        store = FakeVectorStore(self.stored, index)
        catalog = FakeCatalog(self.seed_records)
        service = RetrievalService(
            embedding=FakeEmbedding(),
            vector_store=store,
            catalog=catalog,
            reranker=FakeReranker(),
            config=RetrievalConfig(),
        )
        text_to_id = {c.text: c.chunk_id for c in self.stored}

        per_item: list[dict] = []
        for item in self.items:
            store.predicates_seen.clear()
            response = service.retrieve(
                library_id=item.library,
                version=item.version,
                topic=item.question,
                top_k=max(ks),
                token_budget=10000,
            )
            ranked_ids = [
                text_to_id.get(p.text) for p in response.passages
            ]
            ranked_ids = [cid for cid in ranked_ids if cid]
            gold = self.gold_chunk_id[item.id]
            pred = store.predicates_seen[0] if store.predicates_seen else None
            routed_ok = (
                pred is not None
                and pred.library_id == item.library
                and pred.version_id == item.version
            )
            # belt-and-braces: the metric itself over what was served
            served_ok = version_correctness(response.version, item.version)
            per_item.append(
                {
                    "id": item.id,
                    "library": item.library,
                    "version": item.version,
                    "type": item.type,
                    "version_routing_ok": routed_ok and served_ok,
                    "predicate": (
                        f"{pred.library_id}=={pred.version_id}" if pred else None
                    ),
                    "served_version": response.version,
                    "version_fallback": response.version_fallback,
                    "gold_chunk_id": gold,
                    "ranked_chunk_ids": ranked_ids,
                    "n_passages": len(response.passages),
                    "reciprocal_rank": reciprocal_rank(ranked_ids, gold),
                    "citation_accuracy": (
                        citation_accuracy(response.passages[0].url, response.passages[0].section)
                        if response.passages else 0.0
                    ),
                    **{f"ndcg@{k}": ndcg_at_k(ranked_ids, gold, k) for k in ks},
                    **{f"recall@{k}": recall_at_k(ranked_ids, gold, k) for k in ks},
                }
            )

        report: dict = {
            "branch": "bm25",
            "n_items": len(self.items),
            "note": (
                "BM25-only floor: dense_search returns no hits because no "
                "EmbeddingGemma 2 weights exist on this VM (downloading is "
                "forbidden), and the reranker is a no-op. Treat recall as a "
                "lower bound, not the headline."
            ),
            "recall": {
                f"@{k}": mean([it[f"recall@{k}"] for it in per_item]) for k in ks
            },
            "mrr": mean([it["reciprocal_rank"] for it in per_item]),
            "ndcg": {
                f"@{k}": mean([it[f"ndcg@{k}"] for it in per_item]) for k in ks
            },
            "citation_accuracy": mean([it["citation_accuracy"] for it in per_item]),
            "recall_by_type": {
                t: {
                    f"@{k}": mean(
                        [it[f"recall@{k}"] for it in per_item if it["type"] == t]
                    )
                    for k in ks
                }
                for t in ("how", "changed", "signature")
            },
            "version_routing": {
                "correct": sum(1 for it in per_item if it["version_routing_ok"]),
                "total": len(per_item),
                "rate": mean(
                    [1.0 if it["version_routing_ok"] else 0.0 for it in per_item]
                ),
                "failures": [
                    it["id"] for it in per_item if not it["version_routing_ok"]
                ],
            },
            "chunks": self.chunk_calibration(),
            "items": per_item,
            "blocked": [
                "dense retrieval: no EmbeddingGemma 2 weights on this VM",
                "MiniLM rerank: no MiniLM weights on this VM (no-op reranker used)",
            ],
        }
        return report

    # -- hybrid branch (H200 only) --------------------------------------------
    def run_hybrid(self) -> dict:
        """Dense + MiniLM rerank. Exits BLOCKED when artifacts are absent."""
        problems: list[str] = []
        minilm = os.environ.get("MINILM_MODEL_PATH", "")
        if not (minilm and Path(minilm).is_dir() and any(Path(minilm).iterdir())):
            problems.append(
                "MINILM_MODEL_PATH is unset or not a non-empty directory "
                "(MiniLM reranker weights are not staged on this VM)"
            )
        emb = (
            os.environ.get("MODEL_PATH", "")
            or os.environ.get("EMBEDDING_MODEL_PATH", "")
        )
        if not (emb and Path(emb).is_dir() and any(Path(emb).iterdir())):
            problems.append(
                "MODEL_PATH/EMBEDDING_MODEL_PATH is unset or not a non-empty "
                "directory (EmbeddingGemma 2 weights are not staged on this VM)"
            )
        if problems:
            sys.stderr.write(
                "BLOCKED: --branch hybrid needs model weights that do not exist "
                "on this VM (downloading is forbidden).\n"
            )
            for p in problems:
                sys.stderr.write(f"  - {p}\n")
            sys.stderr.write(
                "Run this branch on the H200 with the artifact manifests from "
                "services/artifacts staged, or run --branch bm25 for the "
                "model-free floor.\n"
            )
            raise SystemExit(2)
        raise NotImplementedError(
            "hybrid wiring (real embedding client + MiniLM reranker) lands "
            "with the H200 run; artifacts are present but the live wiring is "
            "not implemented in this stub."
        )


def build_harness(
    corpus_path: str | Path = CORPUS_FILE,
    docs_dir: str | Path = DOCS_DIR,
    seed_path: str | Path | None = None,
) -> EvalHarness:
    items = load_corpus(corpus_path)
    docs = load_docs(docs_dir)
    if seed_path is None:
        seed_path = MCP_SRC / "mcp_server" / "libraries_seed.json"
    resolver = LibraryResolver.from_seed_file(seed_path)
    return EvalHarness(
        items=items, docs=docs, resolver=resolver, seed_records=resolver._records
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="new_sage eval harness (spec §16)")
    parser.add_argument("--branch", choices=("bm25", "hybrid"), required=True)
    parser.add_argument("--corpus", default=str(CORPUS_FILE))
    parser.add_argument("--docs", default=str(DOCS_DIR))
    parser.add_argument("--seed", default=None)
    parser.add_argument("--resolver-cases", default=str(RESOLVER_CASES_FILE))
    parser.add_argument("--ks", nargs="+", type=int, default=[1, 3, 5, 10])
    parser.add_argument("--report-out", default=None)
    args = parser.parse_args(argv)

    log.info(
        "new_sage eval harness starting: branch=%s corpus=%s docs=%s ks=%s",
        args.branch,
        args.corpus,
        args.docs,
        args.ks,
    )
    started = time.monotonic()

    harness = build_harness(args.corpus, args.docs, args.seed)

    if args.branch == "hybrid":
        harness.run_hybrid()  # exits 2 when blocked
        return 0

    report = harness.run_bm25(tuple(args.ks))
    report["resolver"] = harness.run_resolver_eval(
        load_resolver_cases(args.resolver_cases)
    )
    log.info(
        "eval harness finished: branch=bm25 items=%d elapsed=%.2fs",
        report["n_items"],
        time.monotonic() - started,
    )

    if args.report_out:
        Path(args.report_out).write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        log.info("report written to %s", args.report_out)

    # human summary
    print(f"branch=bm25  items={report['n_items']}")
    for k, v in report["recall"].items():
        print(f"  recall{k}: {v:.3f}")
    print("  recall by type:")
    for t, vals in report["recall_by_type"].items():
        print("    " + t + ": " + ", ".join(f"@{k}={v:.3f}" for k, v in vals.items()))
    vr = report["version_routing"]
    print(f"  version_routing: {vr['correct']}/{vr['total']} = {vr['rate']:.3f}")
    rs = report["resolver"]
    print(f"  resolver_accuracy: {rs['correct']}/{rs['total']} = {rs['accuracy']:.3f}")
    if rs["failures"]:
        print("  resolver failures:")
        for f in rs["failures"]:
            print(
                f"    {f['query']!r} (eco={f['ecosystem']}): expected "
                f"{f['expected']}, got found={f['got_found']} "
                f"id={f['got_library_id']}"
            )
    ch = report["chunks"]
    print(
        f"  chunks: n={ch['n_chunks']} max_tokens={ch['max_tokens']} "
        f"budget<={ch['spec_budget']}: {ch['budget_compliant']} "
        f"code_intact={ch['code_blocks_intact']}"
    )
    print("BLOCKED on H200: " + "; ".join(report["blocked"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
