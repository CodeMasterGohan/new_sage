"""New_sage end-to-end smoke test (spec §1 primary flow, §10, §15).

Proves the full path with fakes only — no network, no Docker, no model
weights, no Postgres:

    seed libraries (SQLite catalog)
      → import_markdown_files → DocChunker → ingest_pages
        (fake embedding client, fake Milvus store, real CatalogWriterAdapter)
      → RetrievalService.retrieve (fake vector store / embedder / reranker)
      → FastMCP tools resolve-library-id + get-library-docs
      → LibraryResolver checks

Run from ~/workspace/new_sage/build with the e2e venv:

    .venv-e2e/bin/python -m pytest tests-e2e -q
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.catalog.models import Base
from services.catalog.repository import CatalogRepository
from services.catalog.writer import CatalogWriterAdapter
from services.ingestion import importers
from services.ingestion.pipeline import (
    EmbeddingClient as IngestEmbeddingClient,
    FakeVectorStore,
    ingest_pages,
)
from services.retrieval.interfaces import (
    EmbeddingClient as RetrievalEmbeddingClient,
    LibraryCatalog,
    LibraryRecord,
    ResolvedVersion,
    Reranker,
    VectorStore,
)
from services.retrieval.models import ScoredHit, StoredChunk
from services.retrieval.predicates import VersionPredicate
from services.retrieval.service import RetrievalService

from mcp_server.clients import (
    InMemoryRetrievalClient,
    RetrievedPassage,
    RetrieveResult,
)
from mcp_server.resolver import LibraryResolver
from mcp_server.server import build_fastmcp_for_tests
from mcp_server.tools import ServiceDeps

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
SEED_JSON = Path(
    "~/workspace/new_sage/build/services/mcp_server/src/mcp_server"
    "/libraries_seed.json"
).expanduser()

# (library_id, [older_version, latest_version]) — ingest order matters:
# activate_version() flips is_latest on every ingest, so the latest
# version must be ingested last.
LIB_VERSIONS = [
    ("fastapi", ["0.115.14", "0.116.1"]),
    ("django", ["5.1.12", "5.2.6"]),
    ("vue", ["3.4.38", "3.5.13"]),
]
LIB_IDS = [lib for lib, _ in LIB_VERSIONS]

EXPECTED_RECORD_KEYS = {
    "chunk_id",
    "library_id",
    "version_id",
    "dense",
    "text",
    "headings",
    "url",
    "token_count",
    "ordinal",
}


def word_len(text: str) -> int:
    """Deterministic token_len stand-in (1 word ~= 1 token)."""
    return len(text.split())


def deterministic_vector(text: str, dim: int = 768) -> list[float]:
    """Deterministic 768-dim vector keyed by the text hash (fake weights)."""
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    x = seed or 1
    out = []
    for _ in range(dim):
        x = (1103515245 * x + 12345) % (2**31)
        out.append(x / 2**31 * 2.0 - 1.0)
    return out


# ----------------------------------------------------------------------
# fakes
# ----------------------------------------------------------------------


class FakeEmbeddingClient(IngestEmbeddingClient):
    """Ingestion-side fake. Mirrors the real /embed_documents HTTP shapes:

    request  {"items": [{"text", "title", "is_code"}], "task": ...}
    response {"vectors": [...]}  (768-dim, order-matched)
    """

    def __init__(self, dim: int = 768) -> None:
        self.dim = dim
        self.payloads: list[dict] = []

    def embed_chunks(self, chunks) -> list[list[float]]:
        items = [
            {
                "text": c.text,
                "title": " > ".join(c.headings),
                "is_code": bool(c.code_language),
            }
            for c in chunks
        ]
        payload = {"items": items, "task": "question_answering"}
        self.payloads.append(payload)
        vectors = [deterministic_vector(c.text, self.dim) for c in chunks]
        response = {"vectors": vectors}  # /embed_documents response shape
        assert all(len(v) == self.dim for v in response["vectors"])
        return response["vectors"]


class FakeRetrievalEmbedding(RetrievalEmbeddingClient):
    def embed_query(self, text: str, task: str = "question_answering") -> list[float]:
        return deterministic_vector(f"{task}:{text}")

    @property
    def dimension(self) -> int:
        return 768


class IdentityReranker(Reranker):
    """Fake MiniLM: identity scores — preserves fusion order."""

    def score(self, query: str, passages) -> list[float]:
        return [1.0 - 1e-9 * i for i in range(len(passages))]


class SmokeVectorStore(VectorStore):
    """Retrieval-side fake over the captured sage_chunks records.

    Applies the SAME VersionPredicate in every branch (§9.2).
    """

    OVERVIEW_TERMS = ("getting started", "quickstart", "overview", "introduction")

    def __init__(self, records: list[dict]) -> None:
        self._records = list(records)

    def _scoped(self, predicate: VersionPredicate) -> list[dict]:
        return [
            r
            for r in self._records
            if r["library_id"] == predicate.library_id
            and r["version_id"] == predicate.version_id
        ]

    @staticmethod
    def _stored(record: dict) -> StoredChunk:
        return StoredChunk(
            chunk_id=record["chunk_id"],
            library_id=record["library_id"],
            version_id=record["version_id"],
            text=record["text"],
            headings=tuple(json.loads(record["headings"])),
            url=record["url"],
            token_count=record["token_count"],
        )

    def dense_search(self, vector, predicate, top_k):
        return [
            ScoredHit(record=self._stored(r), rank=i + 1, score=1.0 / (i + 1))
            for i, r in enumerate(self._scoped(predicate)[:top_k])
        ]

    def bm25_search(self, query_text, predicate, top_k):
        tokens = re.findall(r"[a-z0-9]+", query_text.lower())

        def score(record: dict) -> int:
            hay = (
                record["text"] + " " + " ".join(json.loads(record["headings"]))
            ).lower()
            return sum(hay.count(t) for t in tokens)

        ranked = sorted(self._scoped(predicate), key=score, reverse=True)
        return [
            ScoredHit(record=self._stored(r), rank=i + 1, score=float(score(r)))
            for i, r in enumerate(ranked[:top_k])
        ]

    def identifier_search(self, identifiers, predicate, top_k):
        toks = [t.lower() for t in identifiers]
        hits = [
            r
            for r in self._scoped(predicate)
            if any(
                t in (r["text"] + " ".join(json.loads(r["headings"]))).lower()
                for t in toks
            )
        ]
        return [
            ScoredHit(record=self._stored(r), rank=i + 1, score=1.0)
            for i, r in enumerate(hits[:top_k])
        ]

    def overview_chunks(self, predicate, top_k):
        scoped = sorted(self._scoped(predicate), key=lambda r: r["ordinal"])

        def is_overview(record: dict) -> bool:
            breadcrumb = " / ".join(json.loads(record["headings"])).lower()
            return any(t in breadcrumb for t in self.OVERVIEW_TERMS)

        ordered = [r for r in scoped if is_overview(r)] + [
            r for r in scoped if not is_overview(r)
        ]
        return [self._stored(r) for r in ordered[:top_k]]


class SmokeCatalog(LibraryCatalog):
    """Retrieval-side fake catalog: exact version wins, else latest+fallback."""

    def __init__(self, libs: dict) -> None:
        self._libs = libs

    def get_library(self, library_id: str) -> LibraryRecord | None:
        info = self._libs.get(library_id)
        if info is None:
            return None
        return LibraryRecord(
            library_id=library_id,
            name=info["name"],
            ecosystem=info["ecosystem"],
            aliases=tuple(info["aliases"]),
        )

    def resolve_version(self, library_id: str, version: str | None) -> ResolvedVersion:
        from services.retrieval.errors import VersionResolutionError

        info = self._libs.get(library_id)
        if info is None:
            raise VersionResolutionError(f"unknown library {library_id!r}")
        if version is not None and version in info["versions"]:
            return ResolvedVersion(version_id=version, version=version, fallback=False)
        return ResolvedVersion(
            version_id=info["latest"], version=info["latest"], fallback=True
        )


# ----------------------------------------------------------------------
# fixtures: seed → ingest
# ----------------------------------------------------------------------


@pytest.fixture(scope="session")
def db_url(tmp_path_factory) -> str:
    return f"sqlite:///{tmp_path_factory.mktemp('e2e') / 'sage.db'}"


@pytest.fixture(scope="session")
def seeded_libraries(db_url) -> dict:
    """Seed 3 libraries from libraries_seed.json into the SQLite catalog."""
    seed = json.loads(SEED_JSON.read_text(encoding="utf-8"))
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    libs: dict = {}
    with Session(engine) as session:
        repo = CatalogRepository(session)
        for row in seed["libraries"]:
            if row["library_id"] not in LIB_IDS:
                continue
            lib = repo.create_library(
                name=row["library_id"],
                aliases=row["aliases"],
                ecosystem=row["ecosystem"],
                docs_url=row["docs_url"],
                repo_url=row["repo_url"],
            )
            libs[lib.name] = {
                "name": row["name"],
                "ecosystem": row["ecosystem"],
                "aliases": row["aliases"],
                "versions": row["versions"],
                "latest": row["latest"],
                "docs_url": row["docs_url"],
            }
        session.commit()
    assert set(libs) == set(LIB_IDS), f"seed incomplete: {sorted(libs)}"
    return libs


@pytest.fixture(scope="session")
def ingested(db_url, seeded_libraries, tmp_path_factory):
    """Run the real ingestion pipeline for 3 libs × 2 versions (fakes below it)."""
    store = FakeVectorStore()
    embed = FakeEmbeddingClient()
    writer = CatalogWriterAdapter(db_url, default_source="seed")
    reports: dict = {}
    for library_id, versions in LIB_VERSIONS:
        prev_version = None
        for version in versions:
            md = (FIXTURES / f"{library_id}.md").read_text(
                encoding="utf-8"
            ).replace("{VERSION}", version)
            doc_path = tmp_path_factory.mktemp("docs") / f"{library_id}-{version}.md"
            doc_path.write_text(md, encoding="utf-8")
            pages = importers.import_markdown_files(
                [doc_path], library_id=library_id, version=version, source="seed"
            )
            assert pages, f"no pages imported for {library_id}@{version}"
            for page in pages:
                # Give repo-sourced pages a stable docs URL for provenance.
                page.url = (
                    f"https://docs.example.com/{library_id}/{version}/"
                    f"{Path(page.metadata['repo_path']).stem}"
                )
            report = ingest_pages(
                library_id,
                version,
                pages,
                source="seed",
                prev_version=prev_version,
                embedding_client=embed,
                vector_store=store,
                catalog=writer,
                token_len=word_len,
            )
            assert not report["warnings"], (
                f"unexpected ingest warnings for {library_id}@{version}: "
                f"{report['warnings']}"
            )
            reports[(library_id, version)] = report
            prev_version = version
    return {"records": store.records, "reports": reports, "embed": embed}

# ----------------------------------------------------------------------
# 1. seed assertions
# ----------------------------------------------------------------------


def test_seed_libraries_created(db_url, seeded_libraries):
    assert set(seeded_libraries) == set(LIB_IDS)
    engine = create_engine(db_url)
    with Session(engine) as session:
        repo = CatalogRepository(session)
        for slug in LIB_IDS:
            lib = repo.resolve_library(slug)
            assert lib is not None, f"library {slug!r} missing from catalog"
            assert lib.name == slug
            assert lib.ecosystem  # ecosystem carried over from the seed table
            assert lib.docs_url


# ----------------------------------------------------------------------
# 2. ingest assertions
# ----------------------------------------------------------------------


def test_ingest_produced_chunks_with_breadcrumbs(ingested):
    records = ingested["records"]
    assert records, "no chunks were indexed"
    for library_id, versions in LIB_VERSIONS:
        for version in versions:
            mine = [
                r
                for r in records
                if r["library_id"] == library_id and r["version_id"] == version
            ]
            assert len(mine) >= 2, (
                f"expected >=2 chunks for {library_id}@{version}, got {len(mine)}"
            )
            for r in mine:
                headings = json.loads(r["headings"])
                assert isinstance(headings, list) and headings, (
                    f"chunk {r['chunk_id']} has no heading breadcrumb"
                )
                assert r["token_count"] > 0
                assert r["url"].startswith(
                    f"https://docs.example.com/{library_id}/{version}/"
                )


def test_ingest_code_blocks_intact(ingested):
    records = ingested["records"]
    fenced = [r for r in records if "```" in r["text"]]
    assert fenced, "no chunk preserved a fenced code block"
    assert any("```python" in r["text"] for r in records), "python fence lost"
    assert any("```js" in r["text"] for r in records), "js fence lost"
    # The code content itself survived chunking.
    assert any("def read_root" in r["text"] for r in records)
    assert any("createApp" in r["text"] for r in records)


def test_ingest_milvus_record_shape(ingested):
    for r in ingested["records"]:
        assert set(r.keys()) == EXPECTED_RECORD_KEYS, (
            f"record keys {sorted(r.keys())} != dictated sage_chunks shape"
        )
        assert len(r["dense"]) == 768, "dense vector is not 768-dim"
        # Server-side BM25: no "sparse" in the insert record — the
        # text_to_bm25 function generates it from "text". The BM25 input
        # text must carry headings + code identifiers.
        assert "sparse" not in r
        assert isinstance(r["ordinal"], int)
        assert isinstance(json.loads(r["headings"]), list)


def test_ingest_catalog_rows_and_version_flags(db_url, ingested):
    engine = create_engine(db_url)
    with Session(engine) as session:
        repo = CatalogRepository(session)
        for library_id, versions in LIB_VERSIONS:
            lib = repo.resolve_library(library_id)
            rows = {v.version: v for v in repo.list_versions(lib.id)}
            assert set(rows) == set(versions)
            for version in versions:
                assert rows[version].status == "ready", (
                    f"{library_id}@{version} status={rows[version].status}"
                )
                pages = repo.list_pages(rows[version].id)
                assert pages, f"{library_id}@{version} has no page rows"
                assert repo.count_chunks(rows[version].id) > 0, (
                    f"{library_id}@{version} has no chunk rows"
                )
            latest = [v for v in rows.values() if v.is_latest]
            assert len(latest) == 1 and latest[0].version == versions[-1], (
                f"{library_id}: is_latest wrong: "
                f"{[(v.version, v.is_latest) for v in rows.values()]}"
            )
    # pipeline reports agree
    for (library_id, version), report in ingested["reports"].items():
        assert report["chunks_indexed"] > 0, f"{library_id}@{version} indexed 0 chunks"
        assert report["pages_chunked"] == 1


def test_ingest_embedding_payload_shapes(ingested):
    # The fake mirrored the real /embed_documents HTTP shapes.
    for payload in ingested["embed"].payloads:
        assert set(payload.keys()) == {"items", "task"}
        for item in payload["items"]:
            assert set(item.keys()) == {"text", "title", "is_code"}
            assert item["title"], "heading title missing from embedding item"


# ----------------------------------------------------------------------
# 3. retrieval assertions
# ----------------------------------------------------------------------


@pytest.fixture(scope="session")
def retrieval_service(ingested, seeded_libraries):
    return RetrievalService(
        embedding=FakeRetrievalEmbedding(),
        vector_store=SmokeVectorStore(ingested["records"]),
        catalog=SmokeCatalog(seeded_libraries),
        reranker=IdentityReranker(),
    )


def test_retrieve_version_scoping(retrieval_service):
    old = retrieval_service.retrieve(
        "fastapi", version="0.115.14", topic="path parameters"
    )
    assert old.version == "0.115.14"
    assert old.version_fallback is False
    assert old.passages, "no passages for the old version"
    for p in old.passages:
        assert "0.115.14" in p.text, "v1 passage missing its version marker"
        assert "0.116.1" not in p.text, "v2 content leaked into a v1 query"

    new = retrieval_service.retrieve(
        "fastapi", version="0.116.1", topic="path parameters"
    )
    assert new.version == "0.116.1"
    assert all("0.116.1" in p.text for p in new.passages)
    # The two versions return disjoint content — scoping is real, not cosmetic.
    old_texts = {p.text for p in old.passages}
    new_texts = {p.text for p in new.passages}
    assert old_texts.isdisjoint(new_texts)


def test_retrieve_version_fallback(retrieval_service):
    resp = retrieval_service.retrieve("django", version=None, topic="models")
    assert resp.version == "5.2.6", "None version must fall back to is_latest"
    assert resp.version_fallback is True
    assert resp.passages

    resp = retrieval_service.retrieve("django", version="9.9.9", topic="models")
    assert resp.version == "5.2.6"
    assert resp.version_fallback is True


def test_retrieve_overview_mode(retrieval_service):
    resp = retrieval_service.retrieve("vue", topic=None)
    assert resp.passages, "overview mode returned nothing"
    assert all(p.score is None for p in resp.passages), "overview must skip rerank"
    sections = [" / ".join(p.section).lower() for p in resp.passages]
    assert any("getting started" in s for s in sections), (
        f"overview pages not surfaced first: {sections}"
    )


def test_retrieve_token_budget_keeps_at_least_one(retrieval_service):
    resp = retrieval_service.retrieve("vue", topic="reactivity", token_budget=1)
    assert len(resp.passages) >= 1, "token budget must never empty the response"


# ----------------------------------------------------------------------
# 4. MCP assertions (through the real FastMCP tool surface)
# ----------------------------------------------------------------------


def _mcp_passages(records, library_id, version):
    return [
        RetrievedPassage(
            text=r["text"],
            url=r["url"],
            section=list(json.loads(r["headings"])),
            code_language=None,
            score=0.9,
        )
        for r in records
        if r["library_id"] == library_id and r["version_id"] == version
    ]


@pytest.fixture(scope="session")
def resolver():
    return LibraryResolver.from_seed_file(SEED_JSON)


def _make_mcp(resolver, client):
    return build_fastmcp_for_tests(ServiceDeps(resolver=resolver, retrieval=client))


def _call(mcp, name, args):
    out = asyncio.run(mcp.call_tool(name, args))
    assert len(out) == 1, f"expected one content block from {name}"
    return json.loads(out[0].text)


@pytest.fixture(scope="session")
def mcp_server(resolver, ingested):
    client = InMemoryRetrievalClient(
        _mcp_passages(ingested["records"], "fastapi", "0.116.1")
    )
    return _make_mcp(resolver, client)


def test_mcp_exact_tool_names(mcp_server):
    tools = asyncio.run(mcp_server.list_tools())
    assert sorted(t.name for t in tools) == ["get-library-docs", "resolve-library-id"]


def test_mcp_get_library_docs_provenance(mcp_server):
    result = _call(
        mcp_server,
        "get-library-docs",
        {"library_id": "fastapi", "version": "0.116.1", "topic": "routing"},
    )
    assert result["version_fallback"] is False
    assert result["passages"], "no passages returned"
    for p in result["passages"]:
        text = p["text"]
        assert "<<<sage:passage" in text and "<<<sage:end-passage>>>" in text
        assert 'library="FastAPI"' in text
        assert 'version="0.116.1"' in text
        assert 'url="https://docs.example.com/fastapi/0.116.1/' in text
        assert 'section="' in text
        assert 'trust="untrusted-document-content"' in text


def test_mcp_unknown_library_graceful(mcp_server):
    resolved = _call(
        mcp_server, "resolve-library-id", {"libraryName": "zzzqqqxxx"}
    )
    assert resolved["found"] is False

    docs = _call(mcp_server, "get-library-docs", {"library_id": "zzzqqqxxx"})
    assert docs["passages"] == []
    assert docs["crawl_recommended"] is True
    assert docs["status"], "unknown library should explain itself, not fail"


def test_mcp_unknown_version_fallback(resolver, ingested):
    client = InMemoryRetrievalClient(
        _mcp_passages(ingested["records"], "fastapi", "0.116.1"),
        version_fallback=True,
        status="version '9.9.9' is not indexed; serving '0.116.1' instead",
    )
    result = _call(
        _make_mcp(resolver, client),
        "get-library-docs",
        {"library_id": "fastapi", "version": "9.9.9"},
    )
    assert result["version_fallback"] is True
    assert result["crawl_recommended"] is True
    assert result["passages"], "fallback should still serve indexed passages"


def test_mcp_empty_page_url_does_not_crash(resolver, ingested):
    # §9.3.1 repo-clone imports produce pages with no web URL (url="").
    # The tool must fall back to the library docs_url, not fail validation.
    passages = [
        RetrievedPassage(
            text=p.text, url="", section=p.section, code_language=p.code_language
        )
        for p in _mcp_passages(ingested["records"], "django", "5.2.6")
    ]
    assert passages
    result = _call(
        _make_mcp(resolver, InMemoryRetrievalClient(passages)),
        "get-library-docs",
        {"library_id": "django"},
    )
    assert result["passages"]
    assert 'url="https://docs.djangoproject.com/en/5.2/"' in result["passages"][0]["text"]


# ----------------------------------------------------------------------
# 5. resolver assertions
# ----------------------------------------------------------------------


def test_resolver_exact_name(resolver):
    r = resolver.resolve("FastAPI")
    assert r.found is True and r.library_id == "fastapi"


def test_resolver_alias(resolver):
    r = resolver.resolve("np")
    assert r.found is True and r.library_id == "numpy"


def test_resolver_typo_tolerance(resolver):
    # Truncated/mistyped input: the fuzzy tier auto-accepts a strong hit.
    r = resolver.resolve("fastap")
    assert r.found is True and r.library_id == "fastapi"
