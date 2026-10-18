"""Postgres catalog access: library records, version resolution, aliases.

Two tiny queries against the catalog service's tables (spec §4 / §11) —
no catalog HTTP API is invented. Table/column assumptions (from the
catalog agent's DDL — ``services/catalog/models.py``; verified in Wave 2):

- ``library(id UUID PK, name TEXT UNIQUE, ecosystem TEXT, aliases TEXT[])``
- ``library_version(id UUID PK, library_id UUID FK, version TEXT,
  is_latest BOOLEAN)``

``library_id`` here is the canonical slug (e.g. ``"react"``) — the
catalog's ``Library.name``. The UUID primary keys stay inside Postgres;
the Milvus-facing ``version_id`` is the **version string** (e.g.
``"19.0.0"``), matching what the ingestion pipeline writes into
``sage_chunks`` (and the new_DRUID reference pattern, which predicates on
``document_version`` strings, not surrogate keys).

Resolution rule (§9.2): exact version string -> its version string;
unresolvable or None -> the ``is_latest`` row's version, with
``fallback=True``.
"""

from __future__ import annotations

import logging

from .errors import DependencyError, VersionResolutionError
from .interfaces import LibraryCatalog, LibraryRecord, ResolvedVersion

log = logging.getLogger(__name__)

# Canonical name lookup: the MCP layer passes the slug ("react"), which is
# the catalog's Library.name. Case-insensitive to match
# CatalogRepository.resolve_library's name-tier semantics.
_SQL_LIBRARY_BY_NAME = (
    "SELECT id, name, ecosystem, aliases FROM library "
    "WHERE lower(name) = lower(%s)"
)
# Alias tier (same semantics as the repository): the slug may be a known
# alias of a library whose canonical name differs.
_SQL_LIBRARY_BY_ALIAS = (
    "SELECT id, name, ecosystem, aliases FROM library "
    "WHERE EXISTS (SELECT 1 FROM unnest(aliases) AS a "
    "WHERE lower(a) = lower(%s))"
)
# Exact-version resolution: only the ACTIVE revision (newest READY row)
# is searchable (spec §4 immutability). The Milvus-facing version_id stays
# the version STRING — revisions are a catalog-side concern; retrieval
# predicates never see them.
_SQL_VERSION_EXACT = (
    "SELECT version FROM library_version "
    "WHERE library_id = %s::uuid AND version = %s "
    "AND status = 'ready' "
    "ORDER BY revision DESC LIMIT 1"
)
_SQL_VERSION_LATEST = (
    "SELECT version FROM library_version "
    "WHERE library_id = %s::uuid AND is_latest = TRUE AND status = 'ready' "
    "ORDER BY revision DESC LIMIT 1"
)


class PostgresLibraryCatalog(LibraryCatalog):
    """Catalog backed by Postgres via psycopg (``DATABASE_URL``)."""

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("DATABASE_URL is required")
        self._dsn = dsn

    def _connect(self):
        import psycopg

        # The stack standardizes DATABASE_URL on the SQLAlchemy form
        # (postgresql+psycopg://...); raw psycopg3 does not understand the
        # "+psycopg" driver suffix, so normalize it away.
        dsn = self._dsn.replace("postgresql+psycopg://", "postgresql://")
        return psycopg.connect(dsn)

    def get_library(self, library_id: str) -> LibraryRecord | None:
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(_SQL_LIBRARY_BY_NAME, (library_id,))
                row = cur.fetchone()
                if row is None:
                    cur.execute(_SQL_LIBRARY_BY_ALIAS, (library_id,))
                    row = cur.fetchone()
        except Exception as exc:
            raise DependencyError(f"catalog library lookup failed: {exc}") from exc
        if row is None:
            return None
        _uuid, name, ecosystem, aliases = row
        # The Milvus-facing library_id is the canonical slug (Library.name),
        # not the Postgres UUID — every writer/reader on sage_chunks uses it.
        return LibraryRecord(
            library_id=name or library_id,
            name=name or "",
            ecosystem=ecosystem or "",
            aliases=tuple(a for a in (aliases or ()) if a),
        )

    def _library_uuid(self, library_id: str) -> str | None:
        """Resolve the slug to the catalog's internal UUID (for the FK join)."""
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(_SQL_LIBRARY_BY_NAME, (library_id,))
                row = cur.fetchone()
                if row is None:
                    cur.execute(_SQL_LIBRARY_BY_ALIAS, (library_id,))
                    row = cur.fetchone()
        except Exception as exc:
            raise DependencyError(f"catalog library lookup failed: {exc}") from exc
        return str(row[0]) if row is not None else None

    def resolve_version(
        self, library_id: str, version: str | None
    ) -> ResolvedVersion:
        lib_uuid = self._library_uuid(library_id)
        if lib_uuid is None:
            raise VersionResolutionError(
                f"unknown library {library_id!r}: no catalog row to resolve a version from"
            )
        try:
            with self._connect() as conn, conn.cursor() as cur:
                if version:
                    cur.execute(_SQL_VERSION_EXACT, (lib_uuid, version))
                    row = cur.fetchone()
                    if row is not None:
                        log.debug(
                            "exact version resolved",
                            extra={"library_id": library_id, "version": version},
                        )
                        # version_id IS the version string here: that is what
                        # the ingestion pipeline writes into sage_chunks, so
                        # the VersionPredicate matches indexed rows.
                        return ResolvedVersion(
                            version_id=row[0], version=row[0], fallback=False
                        )
                cur.execute(_SQL_VERSION_LATEST, (lib_uuid,))
                row = cur.fetchone()
        except VersionResolutionError:
            raise
        except Exception as exc:
            raise DependencyError(f"catalog version lookup failed: {exc}") from exc
        if row is None:
            raise VersionResolutionError(
                f"no version resolvable for library {library_id!r}"
                + (f" (requested {version!r})" if version else "")
            )
        log.info(
            "version fallback to is_latest",
            extra={
                "library_id": library_id,
                "requested": version,
                "resolved": row[0],
            },
        )
        # Latest requested implicitly is the expected result, not a fallback.
        return ResolvedVersion(version_id=row[0], version=row[0], fallback=version is not None)
