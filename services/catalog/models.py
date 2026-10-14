"""SQLAlchemy models for the New_sage library catalog (spec §4, §11).

New_sage is a single-user, local-first Context7 alternative: no tenants,
no authentication, no ACLs, no quotas. The catalog is the authority on
what is indexed (libraries, versioned page sets) and what is stale.

Table map (spec §4):

* ``library`` — canonical library record: name, aliases[], ecosystem,
  docs_url, repo_url, resolver_notes.
* ``library_version`` — one immutable (library, version) index. Analogous
  to new_DRUID's ``document_version``. Re-crawls create new rows; old rows
  stay for reproducibility until explicitly pruned (§12).
* ``page`` — one crawled/cloned docs page; ``raw_snapshot_key`` points at
  the raw snapshot in RustFS, ``content_hash`` enables cross-version dedup
  (§6: unchanged pages are aliased, not re-chunked).
* ``chunk`` — one §7 heading chunk; ``headings`` is the section breadcrumb
  rendered in provenance (§15). Vectors live in Milvus, keyed by chunk id.
* ``crawl_job`` — durable background work: on-demand crawls (§5.2),
  repo-clone ingests (§9.3.1), DevDocs syncs (§5.1.1), refresh re-crawls
  (§12). Redis holds the live queue; this table is the durable record.

``TEXT[]`` columns use PostgreSQL ``ARRAY`` in production and fall back to
JSON on SQLite (used by the unit tests).
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

# TEXT[] on PostgreSQL (spec §4), plain JSON elsewhere (SQLite in tests).
TextArray = JSON().with_variant(ARRAY(Text), "postgresql")


class Base(DeclarativeBase):
    pass


class VersionSource(str, enum.Enum):
    """Where a library_version's pages came from (spec §4, §5)."""

    SEED = "seed"  # bulk seed corpus (§5.1)
    CRAWL = "crawl"  # on-demand web crawl (§5.2)
    REPOCLONE = "repoclone"  # git-clone version pinning (§9.3.1)


class VersionStatus(str, enum.Enum):
    """Lifecycle of one immutable (library, version) index (§5, §12).

    Only ``READY`` versions are searchable. Re-crawls create new rows in
    ``PENDING`` and walk them to ``READY``; the old row stays searchable
    until then (stale-while-revalidate, §6).
    """

    PENDING = "pending"  # record exists, nothing fetched yet
    CRAWLING = "crawling"  # §5.2 on-demand crawl in progress
    CLONING = "cloning"  # §9.3.1 repo-clone ingest in progress
    PARSING = "parsing"  # HTML → Markdown extraction (§5.3)
    CHUNKING = "chunking"  # §7 heading chunking
    EMBEDDING = "embedding"  # EmbeddingGemma 2 vectors (§8)
    INDEXING = "indexing"  # Milvus upsert (§9)
    READY = "ready"  # searchable
    FAILED = "failed"


class JobStatus(str, enum.Enum):
    """Lifecycle of a crawl_job row."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Terminal job states: update_job_status stamps finished_at on these.
TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}
)


class JobKind(str, enum.Enum):
    """Kinds of durable background work (spec §5, §12)."""

    CRAWL = "crawl"  # §5.2 on-demand crawl of one (library, version)
    REPOCLONE = "repoclone"  # §9.3.1 git-clone ingest
    DEVDOCS_SYNC = "devdocs_sync"  # §5.1.1 monthly DevDocs image sync
    REFRESH = "refresh"  # §12 stale-while-revalidate re-crawl
    SEED_INGEST = "seed_ingest"  # §5.1 bulk seed ingestion


class Library(Base):
    """A canonical library record (spec §4).

    ``name`` is the canonical identifier used by ``resolve-library-id``.
    ``aliases`` feeds the identifier branch (§9.1): exact/alias match on the
    library name boosts all chunks of the resolved library.
    """

    __tablename__ = "library"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        TextArray, nullable=False, default=list
    )
    ecosystem: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # npm | pypi | crates | ...
    docs_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolver_notes: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # version-detection strategy, scope notes (§6, §9.3)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    versions: Mapped[list["LibraryVersion"]] = relationship(
        back_populates="library",
        cascade="all, delete-orphan",
    )
    jobs: Mapped[list["CrawlJob"]] = relationship(
        back_populates="library",
        cascade="all, delete-orphan",
    )


class LibraryVersion(Base):
    """One immutable index of a (library, version) pair (spec §4).

    Revisions: a re-crawl NEVER mutates this row. It INSERTs a new row
    with ``revision = max(existing) + 1`` for the same
    ``(library_id, version)``. ``UNIQUE(library_id, version, revision)``
    enforces it. Exactly one revision is *active* per (library, version):
    the newest ``READY`` row wins (see
    :meth:`CatalogRepository.get_active_version`); older revisions stay
    byte-identical for reproducibility until explicitly pruned (§12).
    ``is_latest`` marks the version queries fall back to when the
    requested version is unresolvable (§9.2). ``crawled_at +
    refresh_after_days`` drives the stale-while-revalidate sweep (§6, §12),
    which only ever considers the active revision of each version.
    """

    __tablename__ = "library_version"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("library.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )  # spec §4: immutable re-crawl revisions; 1 = first index
    is_latest: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    crawled_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # seed | crawl | repoclone
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=VersionStatus.PENDING.value,
        server_default=VersionStatus.PENDING.value,
    )
    refresh_after_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    commit_sha: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # repo-clone pinning (§9.3.1): git commit SHA of the pinned checkout

    __table_args__ = (
        UniqueConstraint(
            "library_id", "version", "revision",
            name="uq_library_version_revision",
        ),
    )

    library: Mapped["Library"] = relationship(back_populates="versions")
    pages: Mapped[list["Page"]] = relationship(
        back_populates="library_version",
        cascade="all, delete-orphan",
    )


class Page(Base):
    """One crawled/cloned documentation page (spec §4).

    ``raw_snapshot_key`` is the RustFS object key of the raw snapshot
    (§5.3); ``content_hash`` lets a re-crawl alias unchanged pages instead
    of re-chunking them (§6 dedup).
    """

    __tablename__ = "page"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    library_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("library_version.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    section_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_snapshot_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)

    library_version: Mapped["LibraryVersion"] = relationship(back_populates="pages")
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="page",
        cascade="all, delete-orphan",
    )


class Chunk(Base):
    """One §7 heading chunk.

    The dense vector and BM25 tokens live in Milvus keyed by this row's id;
    ``headings`` is the section breadcrumb rendered in provenance (§15) and
    indexed in BM25.
    """

    __tablename__ = "chunk"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    page_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("page.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    headings: Mapped[list[str]] = mapped_column(
        TextArray, nullable=False, default=list
    )

    page: Mapped["Page"] = relationship(back_populates="chunks")


class CrawlJob(Base):
    """Durable record of background crawl/ingest work (spec §5, §12).

    ``library_id``/``version`` are nullable because some jobs (e.g. the
    monthly DevDocs sync) are not scoped to one (library, version).
    """

    __tablename__ = "crawl_job"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("library.id", ondelete="SET NULL"), nullable=True, index=True
    )
    version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=JobStatus.QUEUED.value,
        server_default=JobStatus.QUEUED.value,
        index=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    library: Mapped["Library | None"] = relationship(back_populates="jobs")
