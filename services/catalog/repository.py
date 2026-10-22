"""New_sage catalog repository (spec §4, §9.3, §11).

Single-user service: no tenant scoping, no ACLs, no quotas (spec §13).
Every method takes plain arguments and raises :class:`NotFoundError` /
:class:`ConflictError` from :mod:`.errors` on missing records and
uniqueness violations.

The searchability rule (§9.2, §12) lives here: only ``LibraryVersion``
rows with ``status=ready`` are ever returned by
:meth:`list_ready_versions` / :meth:`resolve_version`. Retrieval must
scope Milvus queries to the resolved version's chunks.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Iterable, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .errors import ConflictError, NotFoundError
from .models import (
    TERMINAL_JOB_STATUSES,
    Chunk,
    CrawlJob,
    JobKind,
    JobStatus,
    Library,
    LibraryVersion,
    Page,
    VersionSource,
    VersionStatus,
)

log = logging.getLogger(__name__)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_aware_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Normalize a DB timestamp to tz-aware UTC.

    SQLite (used in tests) returns naive datetimes for
    ``DateTime(timezone=True)`` columns; Postgres returns aware ones.
    Treat naive values as UTC so staleness arithmetic never mixes
    naive and aware datetimes.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


class CatalogRepository:
    """All catalog access for the New_sage services."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # libraries
    # ------------------------------------------------------------------
    def create_library(
        self,
        *,
        name: str,
        aliases: Iterable[str] = (),
        ecosystem: str | None = None,
        docs_url: str | None = None,
        repo_url: str | None = None,
        resolver_notes: str | None = None,
    ) -> Library:
        """Create a canonical library record (spec §9.3 resolver writes)."""
        existing = self.get_library_by_name(name)
        if existing is not None:
            raise ConflictError(f"library {name!r} already exists")
        library = Library(
            name=name,
            aliases=list(aliases),
            ecosystem=ecosystem,
            docs_url=docs_url,
            repo_url=repo_url,
            resolver_notes=resolver_notes,
        )
        self.session.add(library)
        self.session.flush()
        log.info("catalog: created library %r (id=%s)", name, library.id)
        return library

    def get_library(self, library_id: uuid.UUID) -> Library:
        library = self.session.get(Library, library_id)
        if library is None:
            raise NotFoundError(f"library {library_id} not found")
        return library

    def get_library_by_name(self, name: str) -> Library | None:
        """Exact (case-sensitive) lookup by canonical name."""
        return (
            self.session.execute(select(Library).where(Library.name == name))
            .scalars()
            .one_or_none()
        )

    def resolve_library(
        self, name: str, *, ecosystem: str | None = None
    ) -> Library | None:
        """Identifier-branch resolution, spec §9.3 step 1.

        Case-insensitive exact match on the canonical name first, then on
        ``aliases``. When several alias rows collide and ``ecosystem`` is
        given, the ecosystem-matching row wins.
        """
        needle = name.strip().lower()
        if not needle:
            return None
        candidates: list[Library] = []
        exact = (
            self.session.execute(
                select(Library).where(func.lower(Library.name) == needle)
            )
            .scalars()
            .one_or_none()
        )
        if exact is not None:
            candidates.append(exact)
        for library in self.session.execute(select(Library)).scalars():
            if library in candidates:
                continue
            if any(a.strip().lower() == needle for a in (library.aliases or [])):
                candidates.append(library)
        if not candidates:
            return None
        if ecosystem is not None and len(candidates) > 1:
            eco = ecosystem.strip().lower()
            for library in candidates:
                if (library.ecosystem or "").strip().lower() == eco:
                    return library
        return candidates[0]

    def list_libraries(
        self,
        *,
        ecosystem: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Library]:
        stmt = select(Library).order_by(Library.name).limit(limit).offset(offset)
        if ecosystem is not None:
            stmt = stmt.where(Library.ecosystem == ecosystem)
        return list(self.session.execute(stmt).scalars().all())

    def update_library(self, library_id: uuid.UUID, **fields) -> Library:
        """Update mutable library fields.

        Allowed: name, aliases, ecosystem, docs_url, repo_url,
        resolver_notes. Used by the resolver when it discovers a docs or
        repo URL for an existing record (§9.3).
        """
        allowed = {
            "name",
            "aliases",
            "ecosystem",
            "docs_url",
            "repo_url",
            "resolver_notes",
        }
        library = self.get_library(library_id)
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unknown Library field: {key}")
            if key == "aliases":
                value = list(value)
            setattr(library, key, value)
        self.session.flush()
        return library

    # ------------------------------------------------------------------
    # versions
    # ------------------------------------------------------------------
    def get_or_create_version(
        self,
        library_id: uuid.UUID,
        version: str,
        *,
        source: VersionSource | str,
        refresh_after_days: int = 30,
    ) -> tuple[LibraryVersion, bool]:
        """Return ``(version_row, created)`` for a (library, version) pair.

        Idempotent: the second call with the same arguments returns the
        newest existing revision with ``created=False`` and changes
        nothing. New rows start at ``revision=1`` in ``pending`` with
        ``crawled_at=None``.

        NOTE: this is for first-time indexing and for resolving the
        in-flight row during an ingest. Re-crawls of an already-indexed
        (library, version) must use :meth:`create_version_revision`
        instead — rows are immutable (spec §4).
        """
        self.get_library(library_id)  # existence check
        source_value = VersionSource(source).value
        existing = self.get_version_by_string(library_id, version)
        if existing is not None:
            return existing, False
        row = LibraryVersion(
            library_id=library_id,
            version=version,
            revision=1,
            source=source_value,
            status=VersionStatus.PENDING.value,
            refresh_after_days=refresh_after_days,
        )
        self.session.add(row)
        self.session.flush()
        log.info(
            "catalog: created version row for library_id=%s version=%r revision=1 (source=%s)",
            library_id,
            version,
            source_value,
        )
        return row, True

    def create_version_revision(
        self,
        library_id: uuid.UUID,
        version: str,
        *,
        source: VersionSource | str,
        refresh_after_days: int = 30,
    ) -> LibraryVersion:
        """INSERT a new immutable revision row for a re-crawl (spec §4).

        ``revision`` is ``max(existing revisions for (library_id,
        version)) + 1``. Existing rows are never mutated — the new row
        starts in ``pending`` and is walked through the pipeline statuses
        by the crawler. When no row exists yet this is equivalent to
        creating revision 1.
        """
        self.get_library(library_id)  # existence check
        source_value = VersionSource(source).value
        max_revision = (
            self.session.execute(
                select(func.max(LibraryVersion.revision)).where(
                    LibraryVersion.library_id == library_id,
                    LibraryVersion.version == version,
                )
            ).scalar()
            or 0
        )
        row = LibraryVersion(
            library_id=library_id,
            version=version,
            revision=max_revision + 1,
            source=source_value,
            status=VersionStatus.PENDING.value,
            refresh_after_days=refresh_after_days,
        )
        self.session.add(row)
        self.session.flush()
        log.info(
            "catalog: inserted new immutable revision %d for library_id=%s version=%r (source=%s)",
            row.revision,
            library_id,
            version,
            source_value,
        )
        return row

    def get_version(self, version_id: uuid.UUID) -> LibraryVersion:
        version = self.session.get(LibraryVersion, version_id)
        if version is None:
            raise NotFoundError(f"library version {version_id} not found")
        return version

    def get_version_by_string(
        self, library_id: uuid.UUID, version: str
    ) -> LibraryVersion | None:
        """Newest revision (any status) of a (library, version) pair.

        Ingestion writers use this: during a re-crawl it resolves to the
        in-flight revision row the crawl created. For the searchable row,
        use :meth:`get_active_version`.
        """
        return (
            self.session.execute(
                select(LibraryVersion)
                .where(
                    LibraryVersion.library_id == library_id,
                    LibraryVersion.version == version,
                )
                .order_by(LibraryVersion.revision.desc())
            )
            .scalars()
            .first()
        )

    def get_active_version(
        self, library_id: uuid.UUID, version: str
    ) -> LibraryVersion | None:
        """The active revision of a (library, version) pair (spec §4).

        Exactly one revision is active: the newest ``READY`` row wins.
        Older revisions stay byte-identical for reproducibility but are
        never served. An explicitly requested version string always
        resolves to its active revision (§9.2); ``None`` means nothing is
        indexed for that version yet.
        """
        return (
            self.session.execute(
                select(LibraryVersion)
                .where(
                    LibraryVersion.library_id == library_id,
                    LibraryVersion.version == version,
                    LibraryVersion.status == VersionStatus.READY.value,
                )
                .order_by(LibraryVersion.revision.desc())
            )
            .scalars()
            .first()
        )

    def list_versions(
        self,
        library_id: uuid.UUID,
        *,
        status: VersionStatus | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[LibraryVersion]:
        self.get_library(library_id)  # existence check
        stmt = (
            select(LibraryVersion)
            .where(LibraryVersion.library_id == library_id)
            .order_by(
                LibraryVersion.version.desc(), LibraryVersion.revision.desc()
            )
            .limit(limit)
            .offset(offset)
        )
        if status is not None:
            stmt = stmt.where(
                LibraryVersion.status == VersionStatus(status).value
            )
        return list(self.session.execute(stmt).scalars().all())

    def get_latest_version(self, library_id: uuid.UUID) -> LibraryVersion | None:
        return (
            self.session.execute(
                select(LibraryVersion).where(
                    LibraryVersion.library_id == library_id,
                    LibraryVersion.is_latest.is_(True),
                )
            )
            .scalars()
            .one_or_none()
        )

    def mark_version_ready(
        self, version_id: uuid.UUID, *, make_latest: bool = False
    ) -> LibraryVersion:
        """Mark a version indexed and searchable.

        Sets ``status=ready`` and ``crawled_at=now``. With
        ``make_latest=True``, clears ``is_latest`` on the library's other
        versions and sets it here — exactly one latest per library.
        """
        version = self.get_version(version_id)
        version.status = VersionStatus.READY.value
        version.crawled_at = _utcnow()
        if make_latest:
            # Flip siblings through the ORM (not a bulk Core update) so the
            # session identity map stays consistent for callers holding the
            # old "latest" object.
            siblings = (
                self.session.execute(
                    select(LibraryVersion).where(
                        LibraryVersion.library_id == version.library_id,
                        LibraryVersion.id != version.id,
                        LibraryVersion.is_latest.is_(True),
                    )
                )
                .scalars()
                .all()
            )
            for sibling in siblings:
                sibling.is_latest = False
            version.is_latest = True
        self.session.flush()
        log.info(
            "catalog: version %s@%s revision %d READY%s",
            version.library_id,
            version.version,
            version.revision,
            " (now latest)" if make_latest else "",
        )
        return version

    def update_version_status(
        self, version_id: uuid.UUID, status: VersionStatus | str, **fields
    ) -> LibraryVersion:
        """Advance a version's lifecycle status (§5 pipeline steps)."""
        allowed = {"source", "crawled_at", "refresh_after_days", "is_latest"}
        version = self.get_version(version_id)
        version.status = VersionStatus(status).value
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unknown LibraryVersion field: {key}")
            setattr(version, key, value)
        self.session.flush()
        log.debug(
            "catalog: version %s@%s revision %d -> %s",
            version.library_id,
            version.version,
            version.revision,
            version.status,
        )
        return version

    def delete_version(self, version_id: uuid.UUID) -> None:
        """Prune a version; pages and chunks cascade (§12 version pruning)."""
        version = self.get_version(version_id)
        self.session.delete(version)
        self.session.flush()

    # ------------------------------------------------------------------
    # searchability (§9.2, §12): only READY versions are searchable
    # ------------------------------------------------------------------
    def list_ready_versions(
        self, library_id: uuid.UUID | None = None
    ) -> list[LibraryVersion]:
        """All searchable versions, optionally scoped to one library."""
        stmt = select(LibraryVersion).where(
            LibraryVersion.status == VersionStatus.READY.value
        )
        if library_id is not None:
            stmt = stmt.where(LibraryVersion.library_id == library_id)
        return list(self.session.execute(stmt).scalars().all())

    def resolve_version(
        self, library_id: uuid.UUID, version: str | None
    ) -> tuple[LibraryVersion | None, bool]:
        """Resolve a requested version string to a searchable row.

        Returns ``(row, used_fallback)``. When ``version`` is None or does
        not match a READY row, falls back to the library's ``is_latest``
        READY version with ``used_fallback=True`` (spec §9.2: the MCP tool
        renders a provenance flag in that case). Returns ``(None, True)``
        when nothing is searchable.
        """
        if version is not None:
            row = self.get_active_version(library_id, version)
            if row is not None:
                return row, False
        latest = self.get_latest_version(library_id)
        if latest is not None and latest.status == VersionStatus.READY.value:
            return latest, True
        return None, True

    def list_stale_versions(
        self, *, now: dt.datetime | None = None, limit: int = 100
    ) -> list[LibraryVersion]:
        """READY versions past their refresh horizon (§6, §12).

        A version is stale when ``crawled_at + refresh_after_days < now``.
        Only the active revision of each (library, version) is ever
        considered — superseded revisions are never re-crawled. Ordered
        oldest-crawled first so the refresh sweep (§12) works the backlog
        in priority order.
        """
        now = _as_aware_utc(now) or _utcnow()
        rows = (
            self.session.execute(
                select(LibraryVersion).where(
                    LibraryVersion.status == VersionStatus.READY.value,
                    LibraryVersion.crawled_at.is_not(None),
                    # Manually curated sources have no upstream to refresh.
                    LibraryVersion.source != VersionSource.MANUAL.value,
                )
            )
            .scalars()
            .all()
        )
        # Newest READY revision per (library, version) wins; older
        # revisions are immutable history, not refresh candidates.
        active: dict[tuple[uuid.UUID, str], LibraryVersion] = {}
        for row in rows:
            key = (row.library_id, row.version)
            if key not in active or row.revision > active[key].revision:
                active[key] = row
        stale = []
        for version in active.values():
            crawled = _as_aware_utc(version.crawled_at)
            assert crawled is not None
            if crawled + dt.timedelta(days=version.refresh_after_days) < now:
                stale.append(version)
        stale.sort(key=lambda v: _as_aware_utc(v.crawled_at))
        return stale[:limit]

    # ------------------------------------------------------------------
    # pages + chunks
    # ------------------------------------------------------------------
    def record_page(
        self,
        version_id: uuid.UUID,
        *,
        url: str | None = None,
        title: str | None = None,
        section_path: str | None = None,
        raw_snapshot_key: str | None = None,
        content_hash: str | None = None,
    ) -> Page:
        self.get_version(version_id)  # existence check
        page = Page(
            library_version_id=version_id,
            url=url,
            title=title,
            section_path=section_path,
            raw_snapshot_key=raw_snapshot_key,
            content_hash=content_hash,
        )
        self.session.add(page)
        self.session.flush()
        return page

    def record_pages(
        self, version_id: uuid.UUID, pages: Iterable[Mapping[str, object]]
    ) -> list[Page]:
        """Batch variant of :meth:`record_page` for crawl ingestion."""
        self.get_version(version_id)  # existence check
        rows = [
            Page(
                library_version_id=version_id,
                url=p.get("url"),
                title=p.get("title"),
                section_path=p.get("section_path"),
                raw_snapshot_key=p.get("raw_snapshot_key"),
                content_hash=p.get("content_hash"),
            )
            for p in pages
        ]
        self.session.add_all(rows)
        self.session.flush()
        return rows

    def list_pages(
        self, version_id: uuid.UUID, *, limit: int = 1000, offset: int = 0
    ) -> list[Page]:
        stmt = (
            select(Page)
            .where(Page.library_version_id == version_id)
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.execute(stmt).scalars().all())

    def record_chunks(
        self, page_id: uuid.UUID, chunks: Iterable[Mapping[str, object]]
    ) -> list[Chunk]:
        """Record §7 chunks for a page.

        Each mapping needs ``ordinal`` and ``text``; ``token_count`` and
        ``headings`` (breadcrumb list) are optional.
        """
        page = self.session.get(Page, page_id)
        if page is None:
            raise NotFoundError(f"page {page_id} not found")
        rows = [
            Chunk(
                page_id=page_id,
                ordinal=int(c["ordinal"]),
                text=str(c["text"]),
                token_count=c.get("token_count"),
                headings=list(c.get("headings") or []),
            )
            for c in chunks
        ]
        self.session.add_all(rows)
        self.session.flush()
        return rows

    def list_chunks(self, page_id: uuid.UUID) -> list[Chunk]:
        stmt = (
            select(Chunk)
            .where(Chunk.page_id == page_id)
            .order_by(Chunk.ordinal.asc())
        )
        return list(self.session.execute(stmt).scalars().all())

    def count_chunks(self, version_id: uuid.UUID) -> int:
        """Total chunks indexed for a version (ingestion progress checks)."""
        return (
            self.session.execute(
                select(func.count())
                .select_from(Chunk)
                .join(Page, Page.id == Chunk.page_id)
                .where(Page.library_version_id == version_id)
            ).scalar()
            or 0
        )

    # ------------------------------------------------------------------
    # crawl jobs
    # ------------------------------------------------------------------
    def enqueue_job(
        self,
        *,
        kind: JobKind | str,
        library_id: uuid.UUID | None = None,
        version: str | None = None,
    ) -> CrawlJob:
        """Enqueue durable background work (spec §5.2: crawl instead of fail)."""
        if library_id is not None:
            self.get_library(library_id)  # existence check
        job = CrawlJob(
            library_id=library_id,
            version=version,
            kind=JobKind(kind).value,
            status=JobStatus.QUEUED.value,
        )
        self.session.add(job)
        self.session.flush()
        log.info(
            "catalog: enqueued %s job %s for %s@%s",
            job.kind,
            job.id,
            library_id,
            version,
        )
        return job

    def get_job(self, job_id: uuid.UUID) -> CrawlJob:
        job = self.session.get(CrawlJob, job_id)
        if job is None:
            raise NotFoundError(f"crawl job {job_id} not found")
        return job

    def update_job_status(
        self,
        job_id: uuid.UUID,
        status: JobStatus | str,
        *,
        error: str | None = None,
    ) -> CrawlJob:
        """Advance a job; terminal states stamp ``finished_at``."""
        job = self.get_job(job_id)
        status_value = JobStatus(status).value
        job.status = status_value
        if error is not None:
            job.error = error
        if status_value in TERMINAL_JOB_STATUSES:
            job.finished_at = _utcnow()
        self.session.flush()
        if status_value in TERMINAL_JOB_STATUSES:
            level = log.error if status_value == JobStatus.FAILED.value else log.info
            level(
                "catalog: job %s (%s) -> %s%s",
                job.id,
                job.kind,
                status_value,
                f": {error}" if error else "",
            )
        else:
            log.debug(
                "catalog: job %s (%s) -> %s", job.id, job.kind, status_value
            )
        return job

    def list_jobs(
        self,
        *,
        library_id: uuid.UUID | None = None,
        kind: JobKind | str | None = None,
        status: JobStatus | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CrawlJob]:
        stmt = (
            select(CrawlJob)
            .order_by(CrawlJob.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if library_id is not None:
            stmt = stmt.where(CrawlJob.library_id == library_id)
        if kind is not None:
            stmt = stmt.where(CrawlJob.kind == JobKind(kind).value)
        if status is not None:
            stmt = stmt.where(CrawlJob.status == JobStatus(status).value)
        return list(self.session.execute(stmt).scalars().all())

    def list_pending_jobs(
        self, *, kind: JobKind | str | None = None, limit: int = 100
    ) -> list[CrawlJob]:
        """Queued jobs, FIFO — what the worker pool picks up next."""
        stmt = (
            select(CrawlJob)
            .where(CrawlJob.status == JobStatus.QUEUED.value)
            .order_by(CrawlJob.created_at.asc())
            .limit(limit)
        )
        if kind is not None:
            stmt = stmt.where(CrawlJob.kind == JobKind(kind).value)
        return list(self.session.execute(stmt).scalars().all())
