"""MilvusVectorStore contract tests (Wave 2 wiring).

Covers the ``ordinal`` INT64 field: it must be requested in
``OUTPUT_FIELDS`` and the overview-mode fallback must return rows ordered
by ordinal ascending (pymilvus ``query()`` has no ORDER BY, so the store
sorts client-side).
"""

from __future__ import annotations

import sys
import types

from services.retrieval.milvus_store import (
    OUTPUT_FIELDS,
    MilvusVectorStore,
)
from services.retrieval.predicates import VersionPredicate


def _row(chunk_id, ordinal, **overrides):
    base = {
        "chunk_id": chunk_id,
        "library_id": "react",
        "version_id": "19.0.0",
        "text": "text",
        "headings": '["Guide"]',
        "url": "https://react.dev",
        "token_count": 10,
        "ordinal": ordinal,
    }
    base.update(overrides)
    return base


class _FakeClient:
    def __init__(self, uri=None, token=None):
        self.queries: list[dict] = []

    def query(self, collection_name, filter, output_fields, limit):
        self.queries.append(
            {
                "collection_name": collection_name,
                "filter": filter,
                "output_fields": list(output_fields),
                "limit": limit,
            }
        )
        # First call is the overview-headings query: no matches -> fallback.
        if "headings like" in filter:
            return []
        # Fallback: predicate-only rows in arbitrary (unsorted) order.
        return [
            _row("c3", 3),
            _row("c1", 1),
            _row("c2", 2),
        ]


def _store(monkeypatch):
    module = types.ModuleType("pymilvus")
    module.MilvusClient = _FakeClient
    monkeypatch.setitem(sys.modules, "pymilvus", module)
    return MilvusVectorStore(uri="http://fake:19530")


def test_output_fields_include_ordinal():
    assert "ordinal" in OUTPUT_FIELDS


def test_overview_fallback_orders_by_ordinal_ascending(monkeypatch):
    store = _store(monkeypatch)
    chunks = store.overview_chunks(
        VersionPredicate(library_id="react", version_id="19.0.0"), 10
    )
    assert [c.chunk_id for c in chunks] == ["c1", "c2", "c3"]


def test_overview_fallback_requests_ordinal(monkeypatch):
    store = _store(monkeypatch)
    store.overview_chunks(
        VersionPredicate(library_id="react", version_id="19.0.0"), 10
    )
    fallback = store._client.queries[-1]
    assert "headings like" not in fallback["filter"]
    assert "ordinal" in fallback["output_fields"]
