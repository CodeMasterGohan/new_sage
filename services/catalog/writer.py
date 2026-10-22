"""Real ``CatalogWriter`` backing for the ingestion pipeline (Wave 2 wiring).

This module implements the ingestion service's ``CatalogWriter`` protocol
(``services/ingestion/pipeline.py``) over :class:`CatalogRepository`, so
``pipeline.load_catalog_writer()`` can resolve
``services.catalog.make_catalog_writer`` instead of falling back to the
in-memory ``NullCatalogWriter``.

The protocol is implemented structurally — this module does NOT import the
ingestion package (the dependency runs ingestion -> catalog, never the
reverse). Method set (must track ``pipeline.CatalogWriter``):

- ``page_hashes(library_id, version) -> set[str]``
- ``upsert_page(page, *, chunk_count) -> None``
- ``insert_chunks(chunks) -> int``
- ``alias_page(library_id, from_version, to_version, page) -> None``
- ``activate_version(library_id, version) -> None``

Identity mapping (Wave 2 wiring decision, shared with the retrieval
service's ``PostgresLibraryCatalog``):

- ``library_id`` is the canonical slug (``Library.name``); the UUID primary
  keys stay inside Postgres.
- The Milvus-facing ``version_id`` is the version STRING (``"19.0.0"``),
  matching what the ingestion pipeline writes into ``sage_chunks``.
- Ingestion ``Chunk.page_id`` is the source page's ``content_hash``; the
  adapter maps it to the catalog page UUID via a per-instance cache (and a
  DB lookup fallback).

Sessions: one short-lived ``Session`` per call (the RQ worker runs one job
at a time per process). ``page_hashes`` fails open (returns ``set()``) when
the library/version is unknown — the pipeline treats dedup lookup failure
as non-fatal by design.
"""

from __future__ import annotations

import logging
import os
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .models import Page as PageRow
from .repository import CatalogRepository

log = logging.getLogger(__name__)


class CatalogWriterAdapter:
    """``CatalogWriter`` protocol over :class:`CatalogRepository`."""

    def __init__(self, database_url: str, *, default_source: str = "crawl") -> None:
        if not database_url:
            raise RuntimeError("DATABASE_URL is not set: refusing to guess.")
        self._engine = create_engine(database_url)
        self._default_source = default_source
        # (library_id, version, content_hash) -> page UUID. Populated by
        # upsert_page/alias_page; insert_chunks falls back to a DB lookup
        # when the cache misses (e.g. writer recreated mid-run).
        self._page_ids: dict[tuple[str, str, str], uuid.UUID] = {}

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _library_uuid(self, repo: CatalogRepository, library_id: str) -> uuid.UUID:
        """Resolve the slug to the catalog UUID, creating a stub row when the
        crawler/seed has not registered the library yet."""
        library = repo.resolve_library(library_id)
        if library is None:
            log.info("catalog: auto-creating stub library row for %r", library_id)
            library = repo.create_library(name=library_id)
        return library.id

    def _version_uuid(
        self, repo: CatalogRepository, library_id: str, version: str
    ) -> uuid.UUID:
        lib_uuid = self._library_uuid(repo, library_id)
        row, _ = repo.get_or_create_version(
            lib_uuid, version, source=self._default_source
        )
        return row.id

    def _page_uuid_for_hash(
        self,
        session: Session,
        repo: CatalogRepository,
        library_id: str,
        version: str,
        content_hash: str,
    ) -> uuid.UUID | None:
        key = (library_id, version, content_hash)
        cached = self._page_ids.get(key)
        if cached is not None:
            return cached
        ver_id = self._version_uuid(repo, library_id, version)
        row = (
            session.execute(
                select(PageRow).where(
                    PageRow.library_version_id == ver_id,
                    PageRow.content_hash == content_hash,
                )
            )
            .scalars()
            .one_or_none()
        )
        if row is not None:
            self._page_ids[key] = row.id
            return row.id
        return None

    # ------------------------------------------------------------------
    # CatalogWriter protocol
    # ------------------------------------------------------------------
    def page_hashes(self, library_id: str, version: str) -> set[str]:
        """Content hashes already indexed for (library, version).

        Reads the active (newest READY) revision — dedup is always
        against the searchable index, never an in-flight revision.
        """
        with Session(self._engine) as session:
            repo = CatalogRepository(session)
            library = repo.resolve_library(library_id)
            if library is None:
                return set()
            ver = repo.get_active_version(library.id, version)
            if ver is None:
                return set()
            hashes: set[str] = set()
            offset = 0
            while True:
                pages = repo.list_pages(ver.id, limit=1000, offset=offset)
                if not pages:
                    break
                hashes.update(p.content_hash for p in pages if p.content_hash)
                offset += len(pages)
            return hashes

    def upsert_page(self, page, *, chunk_count: int) -> None:  # noqa: ANN001, ARG002
        """Write the page row (idempotent on (version, content_hash))."""
        with Session(self._engine) as session:
            repo = CatalogRepository(session)
            ver_id = self._version_uuid(repo, page.library_id, page.version)
            existing = (
                session.execute(
                    select(PageRow).where(
                        PageRow.library_version_id == ver_id,
                        PageRow.content_hash == page.content_hash,
                    )
                )
                .scalars()
                .one_or_none()
            )
            if existing is None:
                row = repo.record_page(
                    ver_id,
                    url=page.url,
                    title=page.title,
                    content_hash=page.content_hash,
                    raw_snapshot_key=(page.metadata or {}).get("raw_snapshot_key") or (page.metadata or {}).get("snapshot_key"),
                )
            else:
                row = existing
            self._page_ids[
                (page.library_id, page.version, page.content_hash)
            ] = row.id
            log.debug(
                "catalog: recorded page %s for %s@%s (%d chunks)",
                page.url,
                page.library_id,
                page.version,
                chunk_count,
            )
            session.commit()

    def insert_chunks(self, chunks: list) -> int:  # noqa: ANN001
        """Write chunk metadata rows; return count written."""
        if not chunks:
            return 0
        # Ingestion Chunk.page_id is the source page's content_hash.
        by_page: dict[str, list] = {}
        for chunk in chunks:
            by_page.setdefault(chunk.page_id, []).append(chunk)
        total = 0
        with Session(self._engine) as session:
            repo = CatalogRepository(session)
            for content_hash, group in by_page.items():
                first = group[0]
                page_uuid = self._page_uuid_for_hash(
                    session, repo, first.library_id, first.version_id,
                    content_hash,
                )
                if page_uuid is None:
                    raise RuntimeError(
                        f"catalog has no page row for {first.library_id}@"
                        f"{first.version_id} content_hash={content_hash[:12]}…; "
                        "upsert_page must run before insert_chunks"
                    )
                repo.record_chunks(
                    page_uuid,
                    [
                        {
                            "ordinal": c.chunk_order,
                            "text": c.text,
                            "token_count": c.token_count,
                            "headings": list(c.headings),
                        }
                        for c in group
                    ],
                )
                total += len(group)
            session.commit()
        log.debug(
            "catalog: recorded %d chunk(s) across %d page(s)",
            total,
            len(by_page),
        )
        return total

    def alias_page(
        self,
        library_id: str,
        from_version: str,
        to_version: str,
        page,  # noqa: ANN001
    ) -> None:
        """Record an unchanged page aliased from a previous version (§6).

        Only the page row is aliased — chunk rows are NOT duplicated (the
        page was not re-chunked or re-embedded).
        """
        aliased = _PageShim(
            library_id=library_id,
            version=to_version,
            url=page.url,
            title=page.title,
            content_hash=page.content_hash,
            metadata=page.metadata,
        )
        log.debug(
            "catalog: aliasing page %s from %s to %s",
            page.url, from_version, to_version,
        )
        self.upsert_page(aliased, chunk_count=0)

    def activate_version(self, library_id: str, version: str) -> None:
        """Mark the version ready and latest (exactly one latest per library)."""
        with Session(self._engine) as session:
            repo = CatalogRepository(session)
            ver_id = self._version_uuid(repo, library_id, version)
            row = repo.mark_version_ready(ver_id, make_latest=True)
            session.commit()
            log.info(
                "catalog: activated %s@%s revision %d (ready, latest)",
                library_id,
                version,
                row.revision,
            )


class _PageShim:
    """Minimal duck-type of the ingestion ``Page`` for aliasing."""

    def __init__(self, *, library_id, version, url, title, content_hash, metadata):
        self.library_id = library_id
        self.version = version
        self.url = url
        self.title = title
        self.content_hash = content_hash
        self.metadata = metadata or {}


def make_catalog_writer(
    database_url: str | None = None, *, default_source: str = "crawl"
) -> CatalogWriterAdapter:
    """Build the real catalog writer for the ingestion pipeline.

    Resolves ``DATABASE_URL`` from the argument or the environment; raises
    ``RuntimeError`` when neither is set (the pipeline falls back to
    ``NullCatalogWriter`` with a warning in that case).
    """
    url = database_url or os.environ.get("DATABASE_URL", "")
    return CatalogWriterAdapter(url, default_source=default_source)
