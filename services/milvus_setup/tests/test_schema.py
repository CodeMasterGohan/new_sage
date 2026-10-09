"""Schema assertions for the ``sage_chunks`` collection (spec §9.1).

All assertions run against the plain-data specs and the fake pymilvus
builders — no live Milvus, no real pymilvus required.
"""

from __future__ import annotations

import pytest

from .fake_pymilvus import (
    FakeCollectionSchema,
    FakeFieldSchema,
    FakeFunction,
    FakeIndexParams,
    install_fake_pymilvus,
)
from .. import schema


@pytest.fixture()
def fake_pymilvus(monkeypatch):
    install_fake_pymilvus(monkeypatch)
    yield


def _by_name(instances):
    return {inst.name: inst for inst in instances}


def test_collection_name():
    assert schema.COLLECTION_NAME == "sage_chunks"


def test_field_specs_exact():
    """The §9.1 field list: name, type, and length contract, nothing more."""
    specs = {f["name"]: f for f in schema.FIELD_SPECS}
    assert set(specs) == {
        "chunk_id", "library_id", "version_id", "dense", "sparse",
        "text", "headings", "url", "token_count", "ordinal",
    }

    by_type = {f["name"]: f["dtype"] for f in schema.FIELD_SPECS}
    assert by_type == {
        "chunk_id": "VARCHAR",
        "library_id": "VARCHAR",
        "version_id": "VARCHAR",
        "dense": "FLOAT_VECTOR",
        "sparse": "SPARSE_FLOAT_VECTOR",
        "text": "VARCHAR",
        "headings": "VARCHAR",
        "url": "VARCHAR",
        "token_count": "INT64",
        "ordinal": "INT64",
    }

    pk = [f for f in schema.FIELD_SPECS if f.get("is_primary")]
    assert len(pk) == 1 and pk[0]["name"] == "chunk_id"
    assert pk[0]["max_length"] == 64 and pk[0]["auto_id"] is False

    assert specs["library_id"]["max_length"] == 64
    assert specs["version_id"]["max_length"] == 64
    assert specs["dense"]["dim"] == 768
    assert specs["text"]["max_length"] == 65535
    assert specs["text"]["enable_analyzer"] is True
    assert specs["text"]["analyzer_params"] == {"tokenizer": "standard"}
    assert specs["headings"]["max_length"] == 4096
    assert specs["url"]["max_length"] == 2048

    # Only the PK may carry a primary flag; no partition keys (single user).
    assert all(not f.get("is_partition_key") for f in schema.FIELD_SPECS)


def test_bm25_function_spec():
    spec = schema.BM25_FUNCTION_SPEC
    assert spec["name"] == schema.BM25_FUNCTION_NAME == "text_to_bm25"
    assert spec["function_type"] == "BM25"
    assert spec["input_field_names"] == ["text"]
    assert spec["output_field_names"] == ["sparse"]


def test_index_specs():
    specs = {s["field_name"]: s for s in schema.INDEX_SPECS}
    assert set(specs) == {"dense", "sparse"}

    dense = specs["dense"]
    assert dense["index_type"] == "HNSW"
    assert dense["params"] == {"M": 16, "efConstruction": 200}

    sparse = specs["sparse"]
    assert sparse["index_type"] == "SPARSE_INVERTED_INDEX"
    assert sparse["metric_type"] == "BM25"


def test_build_pymilvus_schema(fake_pymilvus):
    schema.build_pymilvus_schema()
    fields = _by_name(FakeFieldSchema.instances)
    assert set(fields) == {
        "chunk_id", "library_id", "version_id", "dense", "sparse",
        "text", "headings", "url", "token_count", "ordinal",
    }
    assert fields["chunk_id"].kwargs["is_primary"] is True
    assert fields["chunk_id"].kwargs["max_length"] == 64
    assert fields["dense"].kwargs["dim"] == 768
    assert fields["ordinal"].dtype == "DataType.INT64"
    assert fields["text"].kwargs["max_length"] == 65535
    assert fields["text"].kwargs["enable_analyzer"] is True
    assert fields["text"].kwargs["analyzer_params"] == {"tokenizer": "standard"}

    assert len(FakeFunction.instances) == 1
    fn = FakeFunction.instances[0].kwargs
    assert fn["name"] == "text_to_bm25"
    assert fn["function_type"] == "FunctionType.BM25"
    assert fn["input_field_names"] == ["text"]
    assert fn["output_field_names"] == ["sparse"]

    built = FakeCollectionSchema.last
    assert built is not None
    assert built.kwargs.get("enable_dynamic_field") is False


def test_build_index_params(fake_pymilvus):
    params = schema.build_index_params()
    assert FakeIndexParams.last is params
    indexes = {i["field_name"]: i for i in params.indexes}
    assert set(indexes) == {"dense", "sparse"}
    assert indexes["dense"]["index_type"] == "HNSW"
    assert indexes["dense"]["params"] == {"M": 16, "efConstruction": 200}
    assert indexes["sparse"]["index_type"] == "SPARSE_INVERTED_INDEX"
    assert indexes["sparse"]["metric_type"] == "BM25"
