"""Pipeline end-to-end with fakes: chunk -> embed -> Milvus -> catalog."""

from __future__ import annotations

import json

from services.ingestion.doc_chunker import ChunkerSettings
from services.ingestion.models import Page
from services.ingestion.pipeline import (
    EMBEDDING_DIMENSION,
    EmbeddingClient,
    FakeVectorStore,
    NullCatalogWriter,
    ingest_pages,
)

from conftest import word_len

DIM = EMBEDDING_DIMENSION


class FakeEmbedding(EmbeddingClient):
    def __init__(self):
        self.calls: list[list[str]] = []

    def embed_chunks(self, chunks):
        self.calls.append([c.text for c in chunks])
        # Deterministic 768-dim vectors derived from text.
        return [[float((hash(c.text) + i) % 1000) / 1000.0 for i in range(DIM)]
                for c in chunks]


def _settings():
    return ChunkerSettings(target_tokens=40, overlap_tokens=8,
                           hard_budget_tokens=60)


def _page(md: str, url: str = "https://docs.example.com/p") -> Page:
    return Page(library_id="fakelib", version="1.0.0", url=url,
                title="P", markdown=md, source="crawl")


MD = """# Guide

First paragraph about installing the library with pip install fakelib.

Second paragraph about importing and calling run().

## Usage

```python
import fakelib
fakelib.run()
```
"""


def test_end_to_end_fake_backends():
    store, catalog, emb = FakeVectorStore(), NullCatalogWriter(), FakeEmbedding()
    report = ingest_pages(
        "fakelib", "1.0.0", [_page(MD)], "crawl",
        embedding_client=emb, vector_store=store, catalog=catalog,
        token_len=word_len, chunker_settings=_settings(),
    )
    assert report["pages_total"] == 1
    assert report["chunks_indexed"] > 0
    assert emb.calls and len(emb.calls[0]) == report["chunks_indexed"]

    # Milvus record shape matches the sage_chunks schema (server-side BM25:
    # no "sparse" key — the text_to_bm25 function generates it from "text").
    rec = store.records[0]
    assert set(rec) == {"chunk_id", "library_id", "version_id", "dense",
                        "text", "headings", "url", "token_count",
                        "ordinal"}
    assert rec["library_id"] == "fakelib" and rec["version_id"] == "1.0.0"
    assert len(rec["dense"]) == DIM
    assert "sparse" not in rec
    # BM25 input text: chunk text + headings + extracted code identifiers.
    assert "Guide" in rec["text"]  # first chunk's heading folded in
    # Identifier channel: some chunk's BM25 input text carries "run"
    # (extracted from fakelib.run() in the code block).
    assert any("run" in r["text"] for r in store.records)
    json.loads(rec["headings"])  # headings is a JSON string
    assert isinstance(rec["token_count"], int)

    # Catalog rows written; version activated.
    assert catalog.pages and catalog.chunks
    assert ("fakelib", "1.0.0") in catalog.activated


def test_cross_version_dedup_aliases_unchanged_pages():
    store, emb = FakeVectorStore(), FakeEmbedding()
    catalog = NullCatalogWriter()
    # Version 1.0.0: two pages.
    ingest_pages("fakelib", "1.0.0", [_page(MD, "u1"), _page("# Other\n\ntext", "u2")],
                 "crawl", embedding_client=emb, vector_store=store,
                 catalog=catalog, token_len=word_len, chunker_settings=_settings())
    n_vectors_v1 = sum(len(c) for c in emb.calls)

    # Version 1.0.1: page 1 unchanged, page 2 changed.
    store2 = FakeVectorStore()
    emb2 = FakeEmbedding()
    report = ingest_pages(
        "fakelib", "1.0.1", [_page(MD, "u1"), _page("# Other\n\nchanged text", "u2")],
        "crawl", prev_version="1.0.0",
        embedding_client=emb2, vector_store=store2,
        catalog=catalog, token_len=word_len, chunker_settings=_settings(),
    )
    assert report["pages_aliased"] == 1
    assert report["pages_chunked"] == 1
    n_vectors_v2 = sum(len(c) for c in emb2.calls)
    assert n_vectors_v2 < n_vectors_v1, "unchanged page must not be re-embedded"
    assert catalog.aliased == [("1.0.0", "1.0.1")]
    assert ("fakelib", "1.0.1") in catalog.activated


def test_chunk_ids_stable_across_retries():
    store, catalog = FakeVectorStore(), NullCatalogWriter()
    kw = dict(embedding_client=FakeEmbedding(), catalog=catalog,
              token_len=word_len, chunker_settings=_settings())
    ingest_pages("fakelib", "1.0.0", [_page(MD)], "crawl",
                 vector_store=store, **kw)
    ids1 = [r["chunk_id"] for r in store.records]
    store2 = FakeVectorStore()
    ingest_pages("fakelib", "1.0.0", [_page(MD)], "crawl",
                 vector_store=store2, **kw)
    assert [r["chunk_id"] for r in store2.records] == ids1


def test_empty_pages_warns():
    report = ingest_pages("fakelib", "1.0.0", [], "crawl",
                          embedding_client=FakeEmbedding(),
                          vector_store=FakeVectorStore(),
                          catalog=NullCatalogWriter(),
                          token_len=word_len, chunker_settings=_settings())
    assert report["chunks_indexed"] == 0
    assert report["warnings"]


def test_embed_documents_request_shape_matches_service_contract(monkeypatch):
    """Regression: POST /embed_documents must send {"items": [...]}.

    The embedding service's EmbedDocumentsRequest takes "items"; the old
    "documents" key was rejected with 422 (Wave 2 wiring fix).
    """
    import requests as requests_mod

    from services.ingestion.models import Chunk
    from services.ingestion.pipeline import HttpEmbeddingClient

    seen: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"vectors": [[0.1] * DIM], "dimension": DIM}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        return _Resp()

    monkeypatch.setattr(requests_mod, "post", fake_post)
    monkeypatch.setenv("EMBEDDING_URL", "http://embedding:8002")

    chunk = Chunk(
        chunk_id="c_1", library_id="fakelib", version_id="1.0.0",
        page_id="p", url="https://docs.example.com/p", chunk_order=0,
        text="hello", token_count=1, headings=["Guide"],
        code_language="python",
    )
    vectors = HttpEmbeddingClient().embed_chunks([chunk])
    assert len(vectors) == 1
    assert seen["url"] == "http://embedding:8002/embed_documents"
    body = seen["json"]
    assert set(body.keys()) == {"items", "task"}, body.keys()
    assert body["task"] == "question_answering"
    assert body["items"] == [
        {"text": "hello", "title": "Guide", "is_code": True}
    ]


def test_milvus_record_carries_ordinal():
    """Wave 2: sage_chunks records include ordinal INT64 (= chunk_order)."""
    from services.ingestion.models import Chunk

    chunk = Chunk(
        chunk_id="c_1", library_id="fakelib", version_id="1.0.0",
        page_id="p", url="https://docs.example.com/p", chunk_order=3,
        text="hello", token_count=1, headings=["Guide"],
    )
    record = chunk.to_milvus_record()
    assert record["ordinal"] == 3
    assert set(record) == {
        "chunk_id", "library_id", "version_id", "dense",
        "text", "headings", "url", "token_count", "ordinal",
    }
