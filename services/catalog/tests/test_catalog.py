"""Unit tests for the New_sage catalog (spec §4, §9.3, §11).

Run from the build root:  python -m pytest services/catalog/tests -q
Uses SQLite; PostgreSQL-specific behavior (ARRAY columns, server defaults)
is covered by the Alembic migration, which targets PostgreSQL.
"""

import datetime as dt
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.catalog.errors import ConflictError, NotFoundError
from services.catalog.models import (
    Base,
    JobKind,
    JobStatus,
    VersionSource,
    VersionStatus,
)
from services.catalog.repository import CatalogRepository


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def repo(session):
    return CatalogRepository(session)


def make_library(repo, name="react", **kwargs):
    kwargs.setdefault("ecosystem", "npm")
    return repo.create_library(name=name, **kwargs)


def make_version(repo, library_id, version="19.0.0", **kwargs):
    kwargs.setdefault("source", VersionSource.CRAWL)
    row, created = repo.get_or_create_version(library_id, version, **kwargs)
    assert created
    return row


# ---------------------------------------------------------------------------
# libraries
# ---------------------------------------------------------------------------

def test_create_and_get_library(repo):
    lib = make_library(repo, aliases=["reactjs"], docs_url="https://react.dev")
    fetched = repo.get_library(lib.id)
    assert fetched.name == "react"
    assert fetched.aliases == ["reactjs"]
    assert fetched.ecosystem == "npm"
    assert fetched.docs_url == "https://react.dev"
    assert fetched.created_at is not None


def test_create_library_duplicate_name_conflicts(repo):
    make_library(repo, name="react")
    with pytest.raises(ConflictError):
        repo.create_library(name="react")


def test_get_library_missing_raises(repo):
    with pytest.raises(NotFoundError):
        repo.get_library(uuid.uuid4())


def test_get_library_by_name_exact(repo):
    lib = make_library(repo, name="react")
    assert repo.get_library_by_name("react").id == lib.id
    assert repo.get_library_by_name("nope") is None


def test_resolve_library_by_name_case_insensitive(repo):
    lib = make_library(repo, name="react")
    assert repo.resolve_library("React").id == lib.id
    assert repo.resolve_library("  REACT  ").id == lib.id


def test_resolve_library_by_alias(repo):
    lib = make_library(repo, name="python-requests", aliases=["requests"],
                       ecosystem="pypi")
    assert repo.resolve_library("requests").id == lib.id
    assert repo.resolve_library("Requests").id == lib.id


def test_resolve_library_unknown_returns_none(repo):
    make_library(repo, name="react")
    assert repo.resolve_library("definitely-not-a-library") is None
    assert repo.resolve_library("") is None
    assert repo.resolve_library("   ") is None


def test_resolve_library_ecosystem_disambiguates_alias(repo):
    # Two libraries sharing an alias: ecosystem picks the right one.
    py = make_library(repo, name="python-requests", aliases=["requests"],
                      ecosystem="pypi")
    make_library(repo, name="npm-requests", aliases=["requests"],
                 ecosystem="npm")
    assert repo.resolve_library("requests", ecosystem="pypi").id == py.id


def test_resolve_library_prefers_exact_name_over_alias(repo):
    make_library(repo, name="other", aliases=["react"])
    exact = make_library(repo, name="react")
    assert repo.resolve_library("react").id == exact.id


def test_list_libraries_filter_and_paging(repo):
    make_library(repo, name="react", ecosystem="npm")
    make_library(repo, name="requests", ecosystem="pypi")
    make_library(repo, name="vue", ecosystem="npm")
    assert [l.name for l in repo.list_libraries()] == ["react", "requests", "vue"]
    assert {l.name for l in repo.list_libraries(ecosystem="npm")} == {"react", "vue"}
    page = repo.list_libraries(limit=1, offset=1)
    assert len(page) == 1


def test_update_library(repo):
    lib = make_library(repo, name="react")
    updated = repo.update_library(
        lib.id, docs_url="https://react.dev",
        repo_url="https://github.com/facebook/react",
        aliases=["reactjs"],
    )
    assert updated.docs_url == "https://react.dev"
    assert updated.repo_url == "https://github.com/facebook/react"
    assert updated.aliases == ["reactjs"]


def test_update_library_rejects_unknown_field(repo):
    lib = make_library(repo, name="react")
    with pytest.raises(ValueError):
        repo.update_library(lib.id, tenant_id="nope")


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------

def test_get_or_create_version_idempotent(repo):
    lib = make_library(repo)
    v1, created1 = repo.get_or_create_version(
        lib.id, "19.0.0", source=VersionSource.CRAWL)
    v2, created2 = repo.get_or_create_version(
        lib.id, "19.0.0", source="crawl")
    assert created1 is True
    assert created2 is False
    assert v1.id == v2.id
    assert v1.status == VersionStatus.PENDING.value
    assert v1.source == VersionSource.CRAWL.value
    assert v1.is_latest is False
    assert v1.crawled_at is None
    assert v1.refresh_after_days == 30


def test_get_or_create_version_rejects_bad_source(repo):
    lib = make_library(repo)
    with pytest.raises(ValueError):
        repo.get_or_create_version(lib.id, "1.0", source="telepathy")


def test_get_or_create_version_missing_library(repo):
    with pytest.raises(NotFoundError):
        repo.get_or_create_version(uuid.uuid4(), "1.0",
                                   source=VersionSource.SEED)


def test_get_version_missing_raises(repo):
    with pytest.raises(NotFoundError):
        repo.get_version(uuid.uuid4())


def test_list_versions(repo):
    lib = make_library(repo)
    make_version(repo, lib.id, "18.0.0")
    make_version(repo, lib.id, "19.0.0")
    versions = repo.list_versions(lib.id)
    assert {v.version for v in versions} == {"18.0.0", "19.0.0"}
    assert all(v.library_id == lib.id for v in versions)


def test_list_versions_status_filter(repo):
    lib = make_library(repo)
    v = make_version(repo, lib.id, "19.0.0")
    make_version(repo, lib.id, "18.0.0")
    repo.mark_version_ready(v.id)
    ready = repo.list_versions(lib.id, status=VersionStatus.READY)
    assert [r.id for r in ready] == [v.id]


def test_mark_version_ready_sets_crawled_at(repo):
    lib = make_library(repo)
    v = make_version(repo, lib.id, "19.0.0")
    before = dt.datetime.now(dt.timezone.utc)
    repo.mark_version_ready(v.id)
    assert v.status == VersionStatus.READY.value
    assert v.crawled_at is not None
    # SQLite returns naive datetimes; compare in UTC.
    crawled = v.crawled_at.replace(tzinfo=dt.timezone.utc)
    assert crawled >= before


def test_mark_version_ready_make_latest_flips_siblings(repo):
    lib = make_library(repo)
    old = make_version(repo, lib.id, "18.0.0")
    new = make_version(repo, lib.id, "19.0.0")
    repo.mark_version_ready(old.id, make_latest=True)
    assert repo.get_latest_version(lib.id).id == old.id
    repo.mark_version_ready(new.id, make_latest=True)
    assert repo.get_latest_version(lib.id).id == new.id
    # Exactly one latest per library.
    assert repo.get_version(old.id).is_latest is False
    assert repo.get_version(new.id).is_latest is True


def test_update_version_status(repo):
    lib = make_library(repo)
    v = make_version(repo, lib.id, "19.0.0")
    repo.update_version_status(v.id, "crawling")
    assert v.status == VersionStatus.CRAWLING.value
    repo.update_version_status(v.id, VersionStatus.CHUNKING)
    assert v.status == VersionStatus.CHUNKING.value
    with pytest.raises(ValueError):
        repo.update_version_status(v.id, "teleporting")
    with pytest.raises(ValueError):
        repo.update_version_status(v.id, "parsing", bogus_field=1)


def test_list_ready_versions_only_ready(repo):
    lib = make_library(repo)
    ready = make_version(repo, lib.id, "19.0.0")
    pending = make_version(repo, lib.id, "18.0.0")
    failed = make_version(repo, lib.id, "17.0.0")
    repo.mark_version_ready(ready.id)
    repo.update_version_status(failed.id, VersionStatus.FAILED)
    rows = repo.list_ready_versions(lib.id)
    assert [r.id for r in rows] == [ready.id]
    assert pending.id not in [r.id for r in rows]


def test_resolve_version_exact_and_fallback(repo):
    lib = make_library(repo)
    v19 = make_version(repo, lib.id, "19.0.0")
    v18 = make_version(repo, lib.id, "18.3.1")
    repo.mark_version_ready(v19.id, make_latest=True)
    repo.mark_version_ready(v18.id)

    row, fallback = repo.resolve_version(lib.id, "18.3.1")
    assert row.id == v18.id and fallback is False

    # Unknown version falls back to latest (§9.2).
    row, fallback = repo.resolve_version(lib.id, "99.0.0")
    assert row.id == v19.id and fallback is True

    # None requested → latest.
    row, fallback = repo.resolve_version(lib.id, None)
    assert row.id == v19.id and fallback is True

    # A non-ready exact match does NOT resolve; falls back instead.
    pending = make_version(repo, lib.id, "20.0.0-beta")
    row, fallback = repo.resolve_version(lib.id, "20.0.0-beta")
    assert row.id == v19.id and fallback is True


def test_resolve_version_nothing_searchable(repo):
    lib = make_library(repo)
    make_version(repo, lib.id, "19.0.0")  # still pending
    row, fallback = repo.resolve_version(lib.id, "19.0.0")
    assert row is None and fallback is True


def test_list_stale_versions(repo):
    lib = make_library(repo)
    old = make_version(repo, lib.id, "18.0.0", refresh_after_days=30)
    fresh = make_version(repo, lib.id, "19.0.0", refresh_after_days=30)
    pending = make_version(repo, lib.id, "20.0.0", refresh_after_days=1)
    repo.mark_version_ready(old.id)
    repo.mark_version_ready(fresh.id)
    # Backdate the old version past its horizon; fresh stays within it.
    old.crawled_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)
    fresh.crawled_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)

    stale = repo.list_stale_versions()
    assert [v.id for v in stale] == [old.id]
    assert pending.id not in [v.id for v in stale]  # not READY → never stale


def test_list_stale_versions_custom_now(repo):
    lib = make_library(repo)
    v = make_version(repo, lib.id, "19.0.0", refresh_after_days=30)
    repo.mark_version_ready(v.id)
    v.crawled_at = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    # Just inside the horizon…
    assert repo.list_stale_versions(
        now=dt.datetime(2026, 1, 30, tzinfo=dt.timezone.utc)) == []
    # …and just past it.
    stale = repo.list_stale_versions(
        now=dt.datetime(2026, 2, 2, tzinfo=dt.timezone.utc))
    assert [r.id for r in stale] == [v.id]


def test_list_stale_versions_oldest_first(repo):
    lib = make_library(repo)
    newer = make_version(repo, lib.id, "19.0.0", refresh_after_days=7)
    older = make_version(repo, lib.id, "18.0.0", refresh_after_days=7)
    repo.mark_version_ready(newer.id)
    repo.mark_version_ready(older.id)
    now = dt.datetime.now(dt.timezone.utc)
    newer.crawled_at = now - dt.timedelta(days=10)
    older.crawled_at = now - dt.timedelta(days=40)
    stale = repo.list_stale_versions()
    assert [v.id for v in stale] == [older.id, newer.id]


def test_delete_version_cascades(repo):
    lib = make_library(repo)
    v = make_version(repo, lib.id, "19.0.0")
    page = repo.record_page(v.id, url="https://x/y")
    repo.record_chunks(page.id, [{"ordinal": 0, "text": "hello"}])
    repo.delete_version(v.id)
    with pytest.raises(NotFoundError):
        repo.get_version(v.id)
    assert repo.list_pages(v.id) == []
    assert repo.count_chunks(v.id) == 0


# ---------------------------------------------------------------------------
# pages + chunks
# ---------------------------------------------------------------------------

def test_record_page_and_chunks(repo):
    lib = make_library(repo)
    v = make_version(repo, lib.id, "19.0.0")
    page = repo.record_page(
        v.id,
        url="https://react.dev/reference/react",
        title="React API",
        section_path="Reference",
        raw_snapshot_key="snapshots/react/19.0.0/ref.html",
        content_hash="sha256:abc",
    )
    assert page.library_version_id == v.id
    assert page.content_hash == "sha256:abc"

    chunks = repo.record_chunks(page.id, [
        {"ordinal": 1, "text": "second",
         "headings": ["API Reference", "Hooks"]},
        {"ordinal": 0, "text": "first", "token_count": 12,
         "headings": ["API Reference"]},
    ])
    assert len(chunks) == 2
    # list_chunks orders by ordinal.
    listed = repo.list_chunks(page.id)
    assert [c.text for c in listed] == ["first", "second"]
    assert listed[0].token_count == 12
    assert listed[1].headings == ["API Reference", "Hooks"]
    assert repo.count_chunks(v.id) == 2


def test_record_chunks_defaults(repo):
    lib = make_library(repo)
    v = make_version(repo, lib.id, "19.0.0")
    page = repo.record_page(v.id, url="https://x")
    (chunk,) = repo.record_chunks(page.id, [{"ordinal": 0, "text": "t"}])
    assert chunk.token_count is None
    assert chunk.headings == []


def test_record_pages_batch(repo):
    lib = make_library(repo)
    v = make_version(repo, lib.id, "19.0.0")
    pages = repo.record_pages(v.id, [
        {"url": "https://x/a", "title": "A"},
        {"url": "https://x/b", "title": "B", "content_hash": "h"},
    ])
    assert len(pages) == 2
    assert len(repo.list_pages(v.id)) == 2


def test_record_page_missing_version_raises(repo):
    with pytest.raises(NotFoundError):
        repo.record_page(uuid.uuid4(), url="https://x")


def test_record_chunks_missing_page_raises(repo):
    with pytest.raises(NotFoundError):
        repo.record_chunks(uuid.uuid4(), [{"ordinal": 0, "text": "t"}])


# ---------------------------------------------------------------------------
# crawl jobs
# ---------------------------------------------------------------------------

def test_enqueue_and_get_job(repo):
    lib = make_library(repo)
    job = repo.enqueue_job(kind=JobKind.CRAWL, library_id=lib.id,
                           version="19.0.0")
    assert job.status == JobStatus.QUEUED.value
    assert job.kind == JobKind.CRAWL.value
    assert job.finished_at is None
    assert job.error is None
    fetched = repo.get_job(job.id)
    assert fetched.id == job.id


def test_enqueue_job_corpus_wide_allows_nulls(repo):
    # DevDocs sync (§5.1.1) is not scoped to one (library, version).
    job = repo.enqueue_job(kind="devdocs_sync")
    assert job.library_id is None
    assert job.version is None


def test_enqueue_job_rejects_bad_kind(repo):
    with pytest.raises(ValueError):
        repo.enqueue_job(kind="teleport")


def test_enqueue_job_missing_library_raises(repo):
    with pytest.raises(NotFoundError):
        repo.enqueue_job(kind="crawl", library_id=uuid.uuid4())


def test_update_job_status_terminal_stamps_finished_at(repo):
    job = repo.enqueue_job(kind="crawl")
    repo.update_job_status(job.id, JobStatus.RUNNING)
    assert job.status == JobStatus.RUNNING.value
    assert job.finished_at is None  # not terminal
    repo.update_job_status(job.id, "succeeded")
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.finished_at is not None


def test_update_job_status_failure_records_error(repo):
    job = repo.enqueue_job(kind="crawl")
    repo.update_job_status(job.id, JobStatus.FAILED, error="boom")
    assert job.status == JobStatus.FAILED.value
    assert job.error == "boom"
    assert job.finished_at is not None


def test_update_job_status_rejects_bad_status(repo):
    job = repo.enqueue_job(kind="crawl")
    with pytest.raises(ValueError):
        repo.update_job_status(job.id, "vibing")


def test_get_job_missing_raises(repo):
    with pytest.raises(NotFoundError):
        repo.get_job(uuid.uuid4())


def test_list_pending_jobs_fifo(repo):
    j1 = repo.enqueue_job(kind="crawl")
    j2 = repo.enqueue_job(kind="refresh")
    repo.update_job_status(j1.id, JobStatus.RUNNING)
    pending = repo.list_pending_jobs()
    assert [j.id for j in pending] == [j2.id]
    crawl_only = repo.list_pending_jobs(kind=JobKind.CRAWL)
    assert crawl_only == []


def test_list_jobs_filters(repo):
    lib = make_library(repo)
    j1 = repo.enqueue_job(kind="crawl", library_id=lib.id, version="1.0")
    j2 = repo.enqueue_job(kind="devdocs_sync")
    repo.update_job_status(j2.id, JobStatus.SUCCEEDED)
    assert {j.id for j in repo.list_jobs(library_id=lib.id)} == {j1.id}
    assert {j.id for j in repo.list_jobs(kind="devdocs_sync")} == {j2.id}
    assert {j.id for j in repo.list_jobs(status="succeeded")} == {j2.id}
    assert {j1.id, j2.id} == {j.id for j in repo.list_jobs()}


# ---------------------------------------------------------------------------
# immutable revisions (spec §4)
# ---------------------------------------------------------------------------

def _snapshot_row(row):
    """Byte-identical snapshot of a version row's immutable indexed content.

    ``is_latest`` is intentionally excluded: it is a mutable per-library
    pointer (exactly one latest), not part of the immutable index.
    """
    return (
        row.id,
        row.library_id,
        row.version,
        row.revision,
        row.source,
        row.status,
        row.refresh_after_days,
        row.commit_sha,
    )


def test_create_version_revision_increments(repo):
    lib = make_library(repo)
    r1 = repo.create_version_revision(lib.id, "1.0", source=VersionSource.CRAWL)
    assert r1.revision == 1
    assert r1.status == VersionStatus.PENDING.value
    r2 = repo.create_version_revision(lib.id, "1.0", source="crawl")
    assert r2.revision == 2
    assert r2.id != r1.id
    r3 = repo.create_version_revision(lib.id, "2.0", source="seed")
    assert r3.revision == 1  # per (library, version), not global


def test_create_version_revision_missing_library(repo):
    with pytest.raises(NotFoundError):
        repo.create_version_revision(uuid.uuid4(), "1.0",
                                     source=VersionSource.CRAWL)


def test_get_active_version_newest_ready_wins(repo):
    lib = make_library(repo)
    r1 = repo.create_version_revision(lib.id, "1.0", source=VersionSource.CRAWL)
    assert repo.get_active_version(lib.id, "1.0") is None  # none READY yet
    repo.mark_version_ready(r1.id)
    assert repo.get_active_version(lib.id, "1.0").id == r1.id
    r2 = repo.create_version_revision(lib.id, "1.0", source="crawl")
    # In-flight revision does not shadow the searchable one.
    assert repo.get_active_version(lib.id, "1.0").id == r1.id
    repo.mark_version_ready(r2.id)
    assert repo.get_active_version(lib.id, "1.0").id == r2.id
    assert repo.get_active_version(lib.id, "1.0").revision == 2


def test_get_version_by_string_returns_newest_any_status(repo):
    lib = make_library(repo)
    r1 = repo.create_version_revision(lib.id, "1.0", source=VersionSource.CRAWL)
    repo.mark_version_ready(r1.id)
    r2 = repo.create_version_revision(lib.id, "1.0", source="crawl")
    # Ingestion writers resolve the in-flight row, not the searchable one.
    assert repo.get_version_by_string(lib.id, "1.0").id == r2.id


def test_get_or_create_version_idempotent_across_revisions(repo):
    lib = make_library(repo)
    v1, created1 = repo.get_or_create_version(
        lib.id, "1.0", source=VersionSource.CRAWL)
    repo.create_version_revision(lib.id, "1.0", source="crawl")
    v2, created2 = repo.get_or_create_version(
        lib.id, "1.0", source=VersionSource.CRAWL)
    assert created1 is True
    assert created2 is False
    assert v2.revision == 2  # resolves the newest row for the ingest
    assert v1.revision == 1


def test_re_crawl_leaves_old_revision_byte_identical(repo):
    """Spec §4: re-crawls INSERT; the old row is never mutated."""
    lib = make_library(repo)
    old = repo.create_version_revision(lib.id, "1.0", source=VersionSource.CRAWL)
    page = repo.record_page(
        old.id, url="https://docs.example.com/a", title="A",
        content_hash="h1", raw_snapshot_key="snap/1",
    )
    repo.record_chunks(page.id, [{"ordinal": 0, "text": "hello",
                                  "headings": ["Guide"]}])
    repo.mark_version_ready(old.id, make_latest=True)
    before = _snapshot_row(repo.get_version(old.id))
    old_pages = [(p.url, p.title, p.content_hash, p.raw_snapshot_key)
                 for p in repo.list_pages(old.id)]
    old_chunks = [(c.ordinal, c.text, tuple(c.headings))
                  for c in repo.list_chunks(page.id)]

    # The re-crawl path: new revision row, fresh page/chunk rows.
    new = repo.create_version_revision(lib.id, "1.0", source="crawl")
    assert new.revision == 2
    new_page = repo.record_page(
        new.id, url="https://docs.example.com/a", title="A",
        content_hash="h2", raw_snapshot_key="snap/2",
    )
    repo.record_chunks(new_page.id, [{"ordinal": 0, "text": "hello v2",
                                      "headings": ["Guide"]}])
    repo.mark_version_ready(new.id, make_latest=True)

    # Old row byte-identical; old pages/chunks untouched. (is_latest is a
    # mutable per-library pointer, not indexed content: it moves to the
    # new revision by design.)
    assert _snapshot_row(repo.get_version(old.id)) == before
    assert repo.get_version(old.id).is_latest is False
    assert [(p.url, p.title, p.content_hash, p.raw_snapshot_key)
            for p in repo.list_pages(old.id)] == old_pages
    assert [(c.ordinal, c.text, tuple(c.headings))
            for c in repo.list_chunks(page.id)] == old_chunks
    # Retrieval serves the new revision; the old one stays prunable.
    row, fallback = repo.resolve_version(lib.id, "1.0")
    assert row.id == new.id and fallback is False
    assert repo.get_active_version(lib.id, "1.0").id == new.id
    assert repo.get_latest_version(lib.id).id == new.id


def test_list_stale_versions_ignores_superseded_revisions(repo):
    lib = make_library(repo)
    r1 = repo.create_version_revision(
        lib.id, "1.0", source=VersionSource.CRAWL, refresh_after_days=1)
    repo.mark_version_ready(r1.id)
    r1.crawled_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    r2 = repo.create_version_revision(
        lib.id, "1.0", source=VersionSource.CRAWL, refresh_after_days=30)
    repo.mark_version_ready(r2.id)
    r2.crawled_at = dt.datetime.now(dt.timezone.utc)  # fresh
    stale = repo.list_stale_versions()
    # r1 is superseded by r2: only the active revision is a candidate,
    # and r2 is within its horizon → nothing stale.
    assert stale == []


def test_resolve_version_exact_prefers_active_revision(repo):
    lib = make_library(repo)
    r1 = repo.create_version_revision(lib.id, "1.0", source=VersionSource.CRAWL)
    repo.mark_version_ready(r1.id, make_latest=True)
    r2 = repo.create_version_revision(lib.id, "1.0", source="crawl")
    repo.mark_version_ready(r2.id, make_latest=True)
    row, fallback = repo.resolve_version(lib.id, "1.0")
    assert row.id == r2.id
    assert row.revision == 2
    assert fallback is False


def test_unique_constraint_allows_revisions_not_duplicates(repo):
    from sqlalchemy.exc import IntegrityError

    lib = make_library(repo)
    repo.create_version_revision(lib.id, "1.0", source=VersionSource.CRAWL)
    repo.create_version_revision(lib.id, "1.0", source=VersionSource.CRAWL)
    # Same (library, version, revision) twice still violates the constraint.
    dup = repo.create_version_revision(lib.id, "1.0", source=VersionSource.CRAWL)
    dup.revision = 1
    with pytest.raises(IntegrityError):
        repo.session.flush()
