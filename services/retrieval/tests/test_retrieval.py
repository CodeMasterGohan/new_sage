"""Unit tests for the new_sage retrieval service (spec §8, §9).

All collaborators are fakes: no Milvus, no model weights, no Postgres.
"""

from __future__ import annotations

from typing import Sequence

import pytest

from services.retrieval.config import RetrievalConfig
from services.retrieval.errors import (
    DependencyError,
    UnknownLibraryError,
    VersionResolutionError,
)
from services.retrieval.fusion import apply_identifier_boost, reciprocal_rank_fusion
from services.retrieval.identifiers import (
    extract_identifier_tokens,
    library_name_mentions,
)
from services.retrieval.interfaces import (
    EmbeddingClient,
    LibraryCatalog,
    LibraryRecord,
    ResolvedVersion,
    Reranker,
    VectorStore,
)
from services.retrieval.models import (
    FusedCandidate,
    ScoredHit,
    StoredChunk,
    detect_code_language,
)
from services.retrieval.predicates import VersionPredicate
from services.retrieval.prompts import (
    TASK_CODE_RETRIEVAL,
    TASK_QUESTION_ANSWERING,
    TASK_SEARCH_RESULT,
    detect_task,
)
from services.retrieval.reranker import rerank_candidates, truncate_to_budget
from services.retrieval.service import RetrievalService, truncate_to_token_budget


# ----------------------------------------------------------------------
# helpers / fakes
# ----------------------------------------------------------------------
_OVERVIEW_TERMS = (
    "getting started",
    "getting-started",
    "quickstart",
    "quick start",
    "overview",
    "introduction",
)


def make_chunk(
    chunk_id="c1",
    lib="react",
    ver="v19",
    headings=(),
    text=None,
    url="",
    token_count=100,
):
    return StoredChunk(
        chunk_id=chunk_id,
        library_id=lib,
        version_id=ver,
        text=text if text is not None else f"content of {chunk_id}",
        headings=tuple(headings),
        url=url or f"https://docs.example/{lib}/{chunk_id}",
        token_count=token_count,
    )


def hit(record, rank, score=None):
    return ScoredHit(record=record, rank=rank, score=score)


class FakeEmbedding(EmbeddingClient):
    def __init__(self, dim=768):
        self._dim = dim
        self.calls = []  # (text, task)

    def embed_query(self, text, task="question_answering"):
        self.calls.append((text, task))
        return [0.1] * self._dim

    @property
    def dimension(self):
        return self._dim


class FakeStore(VectorStore):
    def __init__(self, dense=(), bm25=(), ident=(), overview=()):
        self._dense = list(dense)
        self._bm25 = list(bm25)
        self._ident = list(ident)
        self._overview = list(overview)
        self.predicates = {}  # branch -> VersionPredicate
        self.top_ks = {}
        self.last_identifiers = None
        self.last_bm25_query = None

    def dense_search(self, vector, predicate, top_k):
        self.predicates["dense"] = predicate
        self.top_ks["dense"] = top_k
        return [hit(r, i + 1) for i, r in enumerate(self._dense[:top_k])]

    def bm25_search(self, query_text, predicate, top_k):
        self.predicates["bm25"] = predicate
        self.top_ks["bm25"] = top_k
        self.last_bm25_query = query_text
        return [hit(r, i + 1) for i, r in enumerate(self._bm25[:top_k])]

    def identifier_search(self, identifiers, predicate, top_k):
        self.predicates["identifier"] = predicate
        self.top_ks["identifier"] = top_k
        self.last_identifiers = list(identifiers)
        return [hit(r, i + 1) for i, r in enumerate(self._ident[:top_k])]

    def overview_chunks(self, predicate, top_k):
        self.predicates["overview"] = predicate
        self.top_ks["overview"] = top_k
        matched = [
            c
            for c in self._overview
            if any(t in " / ".join(c.headings).lower() for t in _OVERVIEW_TERMS)
        ]
        rest = [c for c in self._overview if c not in matched]
        return (matched + rest)[:top_k]


class FakeCatalog(LibraryCatalog):
    """In-memory library + versions; latest resolves with fallback=True."""

    def __init__(self, library=None, versions=None, latest=None):
        self._library = library or LibraryRecord(
            library_id="react", name="react", ecosystem="npm",
            aliases=("reactjs",),
        )
        self._versions = versions or {"19.0.0": "v19", "18.0.0": "v18"}
        self._latest = latest or "v18"
        self._latest_str = {v: k for k, v in self._versions.items()}[self._latest]

    def get_library(self, library_id):
        if self._library and self._library.library_id == library_id:
            return self._library
        return None

    def resolve_version(self, library_id, version):
        if version and version in self._versions:
            return ResolvedVersion(
                version_id=self._versions[version], version=version, fallback=False
            )
        return ResolvedVersion(
            version_id=self._latest, version=self._latest_str, fallback=True
        )


class FakeReranker(Reranker):
    def __init__(self):
        self.seen = []

    def score(self, query, passages):
        self.seen.append((query, list(passages)))
        return [float(len(passages) - i) for i in range(len(passages))]


def make_service(store=None, catalog=None, embedding=None, reranker=None, **cfg_kw):
    return RetrievalService(
        embedding=embedding or FakeEmbedding(),
        vector_store=store or FakeStore(),
        catalog=catalog or FakeCatalog(),
        reranker=reranker or FakeReranker(),
        config=RetrievalConfig(**cfg_kw),
    )


# ----------------------------------------------------------------------
# version predicate: identical on all branches (§9.2)
# ----------------------------------------------------------------------
def test_identical_version_predicate_on_all_branches():
    store = FakeStore(
        dense=[make_chunk("a")],
        bm25=[make_chunk("b")],
        ident=[make_chunk("c")],
    )
    svc = make_service(store=store)
    svc.retrieve("react", version="19.0.0", topic="react hooks useEffect")
    assert set(store.predicates) == {"dense", "bm25", "identifier"}
    expected = VersionPredicate(library_id="react", version_id="v19")
    for branch, predicate in store.predicates.items():
        assert predicate == expected, branch


def test_predicate_milvus_expr():
    p = VersionPredicate(library_id='re"act', version_id="v1")
    expr = p.to_milvus_expr()
    assert expr == 'library_id == "re\\"act" and version_id == "v1"'


# ----------------------------------------------------------------------
# version resolution + fallback flag
# ----------------------------------------------------------------------
def test_exact_version_no_fallback():
    svc = make_service()
    resp = svc.retrieve("react", version="19.0.0", topic="hooks")
    assert resp.version == "19.0.0"
    assert resp.version_fallback is False


def test_unresolvable_version_falls_back_to_latest():
    svc = make_service()
    resp = svc.retrieve("react", version="99.99.9", topic="hooks")
    assert resp.version == "18.0.0"
    assert resp.version_fallback is True


def test_none_version_falls_back_to_latest():
    svc = make_service()
    resp = svc.retrieve("react", topic="hooks")
    assert resp.version == "18.0.0"
    assert resp.version_fallback is True


def test_unknown_library_raises():
    svc = make_service(catalog=FakeCatalog(library=None))
    with pytest.raises(UnknownLibraryError):
        svc.retrieve("nope", topic="hooks")


def test_no_resolvable_version_raises():
    class EmptyCatalog(FakeCatalog):
        def resolve_version(self, library_id, version):
            raise VersionResolutionError("no versions indexed")

    svc = make_service(catalog=EmptyCatalog())
    with pytest.raises(VersionResolutionError):
        svc.retrieve("react", topic="hooks")


# ----------------------------------------------------------------------
# RRF fusion
# ----------------------------------------------------------------------
def test_rrf_orders_by_combined_ranks():
    a, b, c = make_chunk("a"), make_chunk("b"), make_chunk("c")
    fused = reciprocal_rank_fusion(
        {"dense": [hit(a, 1), hit(b, 2)], "bm25": [hit(b, 1), hit(c, 3)]},
        k=60,
    )
    ids = [f.record.chunk_id for f in fused]
    assert ids == ["b", "a", "c"]  # b in both branches -> highest
    assert fused[0].fusion_score == pytest.approx(1 / 62 + 1 / 61)
    assert fused[0].branches == ("bm25", "dense")


def test_rrf_dedupes_by_chunk_id():
    a = make_chunk("a")
    fused = reciprocal_rank_fusion({"dense": [hit(a, 1)], "bm25": [hit(a, 2)]})
    assert len(fused) == 1


def test_identifier_boost_adds_to_existing_and_admits_new():
    a, b = make_chunk("a"), make_chunk("b")
    fused = [FusedCandidate(record=a, fusion_score=0.5, branches=("dense",))]
    out = apply_identifier_boost(fused, [hit(a, 1), hit(b, 5)], boost=1.0)
    by_id = {c.record.chunk_id: c for c in out}
    assert by_id["a"].fusion_score == pytest.approx(1.5)
    assert "identifier" in by_id["a"].branches
    assert by_id["b"].fusion_score == pytest.approx(1.0 + 1 / 65)
    assert out[0].record.chunk_id == "a"


# ----------------------------------------------------------------------
# identifier branch: library-name match boosts the library's chunks
# ----------------------------------------------------------------------
def test_library_name_mentions():
    assert library_name_mentions("react hooks useEffect", "react", ("reactjs",)) == ["react"]
    assert library_name_mentions("REACTJS docs", "react", ("reactjs",)) == ["reactjs"]
    assert library_name_mentions("vue composition api", "react", ("reactjs",)) == []


def test_identifier_branch_fires_on_library_name_mention():
    named = make_chunk("named")
    store = FakeStore(dense=[make_chunk("d")], bm25=[], ident=[named])
    svc = make_service(store=store, identifier_boost=1.0)
    resp = svc.retrieve("react", version="19.0.0", topic="react hooks guide")
    assert "identifier" in store.predicates
    assert store.last_identifiers and "react" in store.last_identifiers
    # Identifier-only hit enters the pool boosted -> ranks first.
    assert resp.passages[0].text == "content of named"


def test_identifier_branch_skipped_without_tokens():
    store = FakeStore(dense=[make_chunk("d")], bm25=[make_chunk("b")])
    svc = make_service(store=store)
    svc.retrieve("react", version="19.0.0", topic="how do hooks work")
    assert "identifier" not in store.predicates


# ----------------------------------------------------------------------
# overview mode (no topic)
# ----------------------------------------------------------------------
def test_overview_mode_prefers_getting_started_pages():
    chunks = [
        make_chunk("api", headings=("API Reference",)),
        make_chunk("start", headings=("Getting Started", "Install")),
        make_chunk("faq", headings=("FAQ",)),
    ]
    store = FakeStore(overview=chunks)
    embedding = FakeEmbedding()
    svc = make_service(store=store, embedding=embedding)
    resp = svc.retrieve("react", version="19.0.0", topic=None)
    assert embedding.calls == []  # no embedding in overview mode
    assert "overview" in store.predicates
    assert [p.text for p in resp.passages][0] == "content of start"
    assert all(p.score is None for p in resp.passages)


def test_overview_mode_falls_back_to_store_order():
    chunks = [make_chunk("x", headings=("API",)), make_chunk("y", headings=("CLI",))]
    store = FakeStore(overview=chunks)
    svc = make_service(store=store)
    resp = svc.retrieve("react", topic="")
    assert [p.text for p in resp.passages] == ["content of x", "content of y"]


def test_overview_mode_respects_top_k():
    chunks = [make_chunk(f"c{i}") for i in range(10)]
    store = FakeStore(overview=chunks)
    svc = make_service(store=store)
    resp = svc.retrieve("react", top_k=3)
    assert len(resp.passages) == 3


# ----------------------------------------------------------------------
# token-budget truncation
# ----------------------------------------------------------------------
def test_token_budget_truncation():
    chunks = [make_chunk(f"c{i}", token_count=400) for i in range(5)]
    store = FakeStore(dense=chunks)
    svc = make_service(store=store)
    resp = svc.retrieve("react", topic="hooks", top_k=5, token_budget=1000)
    total = sum(p.token_count for p in resp.passages)
    assert total <= 1000
    assert len(resp.passages) == 2  # 400 + 400 fits; third would exceed
    assert len(resp.passages) >= 1  # never empty when retrieval found chunks


def test_token_budget_uses_estimate_when_count_missing():
    chunk = make_chunk("e", token_count=0, text="x" * 400)  # ~100 tokens est.
    store = FakeStore(dense=[chunk, make_chunk("f", token_count=0, text="y" * 400)])
    svc = make_service(store=store)
    resp = svc.retrieve("react", topic="hooks", token_budget=150)
    assert len(resp.passages) == 1
    assert resp.passages[0].token_count == 100


def test_truncate_to_token_budget_always_keeps_first():
    from services.retrieval.models import Passage

    big = Passage(text="t", url="u", section=[], code_language=None, score=1.0,
                  token_count=99999)
    assert truncate_to_token_budget([big], 10) == [big]
    assert truncate_to_token_budget([], 10) == []


# ----------------------------------------------------------------------
# response contract
# ----------------------------------------------------------------------
def test_response_contract_fields_exact():
    store = FakeStore(dense=[make_chunk("a", text="```python\nx = 1\n```")])
    svc = make_service(store=store)
    resp = svc.retrieve("react", version="19.0.0", topic="hooks")
    d = resp.to_dict()
    assert set(d) == {"library", "version", "version_fallback", "passages"}
    assert d["library"] == {"library_id": "react", "name": "react", "ecosystem": "npm"}
    (passage,) = d["passages"]
    assert set(passage) == {"text", "url", "section", "code_language", "score"}
    assert passage["code_language"] == "python"
    assert isinstance(passage["score"], float)


def test_detect_code_language():
    assert detect_code_language("```js\nfoo();\n```") == "js"
    assert detect_code_language("no fences here") is None
    assert detect_code_language("```C++\nint x;\n```") == "c++"


def test_finalists_capped_by_top_k():
    chunks = [make_chunk(f"c{i}") for i in range(20)]
    store = FakeStore(dense=chunks)
    svc = make_service(store=store)
    resp = svc.retrieve("react", topic="hooks", top_k=5)
    assert len(resp.passages) == 5


# ----------------------------------------------------------------------
# embedding contract (§8): raw text + task string, width fail-closed
# ----------------------------------------------------------------------
def test_embedding_receives_raw_text_and_task():
    embedding = FakeEmbedding()
    svc = make_service(embedding=embedding)
    svc.retrieve("react", version="19.0.0", topic="how does `parse_csv(path)` work?")
    (text, task) = embedding.calls[0]
    assert text == "how does `parse_csv(path)` work?"
    assert task == TASK_CODE_RETRIEVAL  # no client-side prompt prefixing


def test_detect_task_code_heuristic_and_hint_override():
    assert detect_task("how does `parse_csv(path)` handle headers?") == TASK_CODE_RETRIEVAL
    assert detect_task("what is the return policy?") == TASK_QUESTION_ANSWERING
    assert detect_task("def foo(): pass", task_hint=TASK_SEARCH_RESULT) == TASK_SEARCH_RESULT


def test_embedding_width_mismatch_fails_closed():
    svc = make_service(embedding=FakeEmbedding(dim=512))
    with pytest.raises(DependencyError):
        svc.retrieve("react", topic="hello")


# ----------------------------------------------------------------------
# identifier token extraction + reranker plumbing
# ----------------------------------------------------------------------
def test_extract_identifier_tokens():
    tokens = extract_identifier_tokens("status of invoice INV-2024-001 and § 4.2?")
    assert "INV-2024-001" in tokens
    assert "§ 4.2" in tokens
    assert extract_identifier_tokens("what is the refund policy") == []


def test_reranker_scores_in_order_and_reranked_desc():
    a, b = make_chunk("a"), make_chunk("b")
    store = FakeStore(dense=[a, b], bm25=[b, a])
    reranker = FakeReranker()
    svc = make_service(store=store, reranker=reranker)
    resp = svc.retrieve("react", topic="hooks")
    # FakeReranker scores descending by input (shortlist) order; input order
    # is the fused order.
    scores = [p.score for p in resp.passages]
    assert scores == sorted(scores, reverse=True)
    assert len(reranker.seen) == 1


def test_rerank_candidates_rejects_score_mismatch():
    class BadReranker(Reranker):
        def score(self, query: str, passages: Sequence[str]):
            return [1.0, 2.0]  # 2 scores for 1 candidate

    a = make_chunk("a")
    with pytest.raises(ValueError):
        rerank_candidates("q", [FusedCandidate(record=a, fusion_score=0.1)], BadReranker())


def test_truncate_to_budget():
    long_text = "word " * 2000
    out = truncate_to_budget(long_text)
    assert len(out) <= 420 * 4


# ----------------------------------------------------------------------
# catalog failure fails closed
# ----------------------------------------------------------------------
def test_catalog_failure_fails_closed():
    class BrokenCatalog(FakeCatalog):
        def get_library(self, library_id):
            raise RuntimeError("db down")

    svc = make_service(catalog=BrokenCatalog())
    with pytest.raises(DependencyError):
        svc.retrieve("react", topic="hooks")
