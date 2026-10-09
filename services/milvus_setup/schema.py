"""Milvus schema definition for New_sage — spec §9.1 (v1).

Single-user Context7 alternative: no tenants, no partitions, no aliases.
Collection ``sage_chunks`` holds heading-based doc chunks with hybrid
retrieval (dense + BM25 + identifier branch upstream in the retrieval
service).

The schema is expressed as plain data (``FIELD_SPECS`` /
``BM25_FUNCTION_SPEC`` / ``INDEX_SPECS``) so tests and tooling can inspect
it without importing pymilvus; :func:`build_pymilvus_schema` /
:func:`build_index_params` translate it into the pinned pymilvus SDK
(2.5.x) with lazy imports.
"""

from __future__ import annotations

COLLECTION_NAME = "sage_chunks"

DENSE_DIM = 768  # EmbeddingGemma 2 unified embedding space (spec §8)
BM25_FUNCTION_NAME = "text_to_bm25"

# Opaque ID contract shared with catalog / retrieval / MCP server:
# ^[A-Za-z0-9_-]{1,64}$ (matches VARCHAR(64) fields).
ID_MAX_LENGTH = 64

# Plain-data field specs: (name, dtype-name, kwargs). dtype names map to
# pymilvus.DataType members in build_pymilvus_schema().
FIELD_SPECS: tuple[dict, ...] = (
    {"name": "chunk_id", "dtype": "VARCHAR", "max_length": 64,
     "is_primary": True, "auto_id": False,
     "description": "Chunk primary key (<library>:<version>:<page-ordinal>)"},
    {"name": "library_id", "dtype": "VARCHAR", "max_length": 64,
     "description": "Library lookup, resolver boosting, library-scoped deletion"},
    {"name": "version_id", "dtype": "VARCHAR", "max_length": 64,
     "description": "Version scoping on all three retrieval branches (spec §9.2); version-scoped deletion"},
    {"name": "dense", "dtype": "FLOAT_VECTOR", "dim": DENSE_DIM,
     "description": "EmbeddingGemma 2 semantic vector (768-dim)"},
    {"name": "sparse", "dtype": "SPARSE_FLOAT_VECTOR",
     "description": "BM25-generated sparse representation"},
    {"name": "text", "dtype": "VARCHAR", "max_length": 65535,
     "enable_analyzer": True, "analyzer_params": {"tokenizer": "standard"},
     "description": "Retrieval text incl. headings + code identifiers (analyzer-enabled for BM25)"},
    {"name": "headings", "dtype": "VARCHAR", "max_length": 4096,
     "description": "Section breadcrumb, e.g. ['API Reference', 'Client', 'connect()']"},
    {"name": "url", "dtype": "VARCHAR", "max_length": 2048,
     "description": "Source page URL (provenance)"},
    {"name": "token_count", "dtype": "INT64",
     "description": "Chunk token count (token-budget accounting for get-library-docs)"},
    {"name": "ordinal", "dtype": "INT64",
     "description": "Chunk order within its page (Wave 2): deterministic "
                    "ordering for the overview-mode fallback (order by ordinal ASC)"},
)

# BM25 function spec (spec §9.1): explicit function, text -> sparse.
# The sparse index must use the BM25 metric, NOT IP.
BM25_FUNCTION_SPEC: dict = {
    "name": BM25_FUNCTION_NAME,
    "function_type": "BM25",
    "input_field_names": ["text"],
    "output_field_names": ["sparse"],
}

# Dense uses COSINE (matches new_DRUID v2.1's EmbeddingGemma 2 space).
INDEX_SPECS: tuple[dict, ...] = (
    {"field_name": "dense", "index_type": "HNSW", "metric_type": "COSINE",
     "params": {"M": 16, "efConstruction": 200}},
    {"field_name": "sparse", "index_type": "SPARSE_INVERTED_INDEX",
     "metric_type": "BM25", "params": {}},
)


def field_names() -> list[str]:
    return [f["name"] for f in FIELD_SPECS]


def build_pymilvus_schema():
    """Translate FIELD_SPECS into a pymilvus CollectionSchema (lazy import)."""
    from pymilvus import CollectionSchema, DataType, FieldSchema, Function, FunctionType

    fields = []
    for spec in FIELD_SPECS:
        dtype = DataType[spec["dtype"]]
        kwargs: dict = {"description": spec.get("description", "")}
        for key in ("max_length", "dim", "is_primary", "auto_id",
                    "is_partition_key", "nullable", "enable_analyzer",
                    "analyzer_params", "max_capacity"):
            if key in spec:
                kwargs[key] = spec[key]
        if spec["dtype"] == "ARRAY":
            kwargs["element_type"] = DataType[spec["element_type"]]
        fields.append(FieldSchema(name=spec["name"], dtype=dtype, **kwargs))

    fn_spec = BM25_FUNCTION_SPEC
    functions = [Function(
        name=fn_spec["name"],
        function_type=FunctionType[fn_spec["function_type"]],
        input_field_names=fn_spec["input_field_names"],
        output_field_names=fn_spec["output_field_names"],
    )]
    return CollectionSchema(
        fields,
        functions=functions,
        description="New_sage doc chunks — hybrid dense+BM25 retrieval (spec §9.1)",
        enable_dynamic_field=False,
    )


def build_index_params():
    """Translate INDEX_SPECS into a pymilvus IndexParams (lazy import)."""
    from pymilvus.milvus_client.index import IndexParams

    index_params = IndexParams()
    for spec in INDEX_SPECS:
        index_params.add_index(
            field_name=spec["field_name"],
            index_type=spec["index_type"],
            metric_type=spec["metric_type"],
            params=spec["params"],
        )
    return index_params
