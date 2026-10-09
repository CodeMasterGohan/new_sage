"""Library-scoped Milvus deletion helpers — spec §12 (version pruning).

Pruning is authorized in the catalog first; these helpers execute the
Milvus side. They always include the library predicate — there is no code
path that deletes without ``library_id``. ``delete_version`` narrows it
further with ``version_id``.

Note on visibility: Milvus deletes go through compaction, so a deleted
chunk can remain briefly visible in the index. Retrieval must ALSO apply
the catalog-layer hard filter; this module only guarantees the delete is
issued with the correct library scope.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .schema import COLLECTION_NAME

log = logging.getLogger("milvus_setup")

# Opaque ID contract (same as catalog / retrieval / MCP server):
# ^[A-Za-z0-9_-]{1,64}$ for library slugs; version_id additionally allows
# dots because version_id is the version STRING (e.g. "19.0.0") — that is
# what the ingestion pipeline writes into sage_chunks (Wave 2 wiring
# decision). Caller IDs that do not match are rejected instead of being
# interpolated into an expression. Matches the VARCHAR(64) fields.
# (Dots are safe inside the double-quoted Milvus string literals below.)
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_OPAQUE_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _validate_id(label: str, value: str, *, version: bool = False) -> str:
    pattern = _OPAQUE_VERSION_RE if version else _OPAQUE_ID_RE
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError(
            f"invalid {label}: must match {pattern.pattern!r}; "
            "refusing to build a delete expression"
        )
    return value


def build_delete_expr_library(library_id: str) -> str:
    """Delete expression for every chunk of one library.

    Raises:
        ValueError: if ``library_id`` fails the opaque-ID contract.
            Fail closed — never interpolate unvalidated input.
    """
    _validate_id("library_id", library_id)
    # The charset above contains no quotes or backslashes, so interpolation
    # into a double-quoted Milvus string literal is safe.
    return f'library_id == "{library_id}"'


def build_delete_expr_version(library_id: str, version_id: str) -> str:
    """Delete expression for every chunk of one (library, version) pair.

    Raises:
        ValueError: if either ID fails the opaque-ID contract.
    """
    _validate_id("library_id", library_id)
    _validate_id("version_id", version_id, version=True)
    return f'library_id == "{library_id}" and version_id == "{version_id}"'


@dataclass
class DeleteResult:
    """Outcome of a library-scoped deletion from Milvus."""

    library_id: str
    version_id: str | None
    collection: str
    delete_count: int
    expr: str


def delete_library(
    client,
    library_id: str,
    collection: str = COLLECTION_NAME,
) -> DeleteResult:
    """Delete every chunk of ``library_id``.

    Args:
        client: a connected ``pymilvus.MilvusClient`` (or a test double
            exposing ``delete(collection_name, filter)`` returning an object
            with ``delete_count``).
        library_id: opaque library ID (validated against the ID contract).
        collection: collection to delete through (default ``sage_chunks``).

    Returns:
        DeleteResult with the server-reported delete count.
    """
    expr = build_delete_expr_library(library_id)
    log.info(
        "deleting all chunks of library %r from %r (expr: %s)",
        library_id,
        collection,
        expr,
    )
    result = client.delete(collection_name=collection, filter=expr)
    count = result.get("delete_count", 0) if isinstance(result, dict) else getattr(
        result, "delete_count", 0
    )
    log.info(
        "deleted %d chunk(s) of library %r from %r",
        int(count),
        library_id,
        collection,
    )
    return DeleteResult(
        library_id=library_id,
        version_id=None,
        collection=collection,
        delete_count=int(count),
        expr=expr,
    )


def delete_version(
    client,
    library_id: str,
    version_id: str,
    collection: str = COLLECTION_NAME,
) -> DeleteResult:
    """Delete every chunk of one (library, version) pair — §12 pruning.

    Args:
        client: a connected ``pymilvus.MilvusClient`` (or a test double
            exposing ``delete(collection_name, filter)`` returning an object
            with ``delete_count``).
        library_id: opaque library ID (validated against the ID contract).
        version_id: version string, e.g. "19.0.0" (validated; dots allowed).
        collection: collection to delete through (default ``sage_chunks``).

    Returns:
        DeleteResult with the server-reported delete count.
    """
    expr = build_delete_expr_version(library_id, version_id)
    log.info(
        "deleting all chunks of %s@%s from %r (expr: %s)",
        library_id,
        version_id,
        collection,
        expr,
    )
    result = client.delete(collection_name=collection, filter=expr)
    count = result.get("delete_count", 0) if isinstance(result, dict) else getattr(
        result, "delete_count", 0
    )
    log.info(
        "deleted %d chunk(s) of %s@%s from %r",
        int(count),
        library_id,
        version_id,
        collection,
    )
    return DeleteResult(
        library_id=library_id,
        version_id=version_id,
        collection=collection,
        delete_count=int(count),
        expr=expr,
    )
