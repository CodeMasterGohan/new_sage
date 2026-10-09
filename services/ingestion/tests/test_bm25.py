"""BM25 input-text contract (server-side BM25).

The Milvus ``sage_chunks`` collection's server-side BM25 function
(``text_to_bm25``) reads the ``text`` field and generates ``sparse``
automatically — ingestion never computes or writes sparse vectors. These
tests pin the NEW contract:

* ``models.index_text_for_chunk`` composes the BM25 input text as
  chunk text + headings + extracted code identifiers (headings and
  identifiers repeated for weight, per the old local-BM25 composition).
* ``Chunk.to_milvus_record()`` puts that composition in the ``text``
  field and carries NO ``sparse`` key.
"""

from __future__ import annotations

from services.ingestion.models import Chunk, index_text_for_chunk


def _chunk(text: str, headings=(), identifiers=()) -> Chunk:
    return Chunk(
        chunk_id="c_x", library_id="l", version_id="v", page_id="p", url="u",
        chunk_order=0, text=text, token_count=0,
        headings=list(headings), identifiers=list(identifiers),
    )


def test_index_text_contains_all_three_channels():
    composed = index_text_for_chunk(
        "connect opens a connection",
        ["API", "connect"],
        ["connect", "Connection"],
    )
    assert "connect opens a connection" in composed
    assert "API" in composed and "connect" in composed
    assert "Connection" in composed


def test_headings_and_identifiers_repeated_for_weight():
    composed = index_text_for_chunk("body text", ["API"], ["connect"])
    # Headings appear twice, identifiers three times (weighting trick
    # carried over from the old local-BM25 composition).
    assert composed.count("API") == 2
    assert composed.count("connect") == 3
    # Raw text appears once.
    assert composed.count("body text") == 1


def test_empty_channels_degrade_gracefully():
    composed = index_text_for_chunk("just text", [], [])
    assert "just text" in composed


def test_milvus_record_has_no_sparse_key():
    rec = _chunk("hello", ["Guide"], ["run"]).to_milvus_record()
    assert "sparse" not in rec, (
        "sparse is function-generated server-side; the insert record "
        "must not carry it"
    )


def test_milvus_record_text_is_bm25_composition():
    chunk = _chunk(
        "connect opens a connection to the server",
        headings=["API", "connect"],
        identifiers=["connect"],
    )
    rec = chunk.to_milvus_record()
    assert rec["text"] == index_text_for_chunk(
        chunk.text, chunk.headings, chunk.identifiers
    )
    # The identifier channel survived into the BM25 input text.
    assert "connect" in rec["text"]
    assert "API" in rec["text"]


def test_milvus_record_shape_matches_function_wired_schema():
    rec = _chunk("hello").to_milvus_record()
    assert set(rec) == {
        "chunk_id", "library_id", "version_id", "dense",
        "text", "headings", "url", "token_count", "ordinal",
    }
