"""Tests for jobs.py (offline: real SQLite catalog, fake fetcher/git/ingest).

The fake ingest drives the RepoCatalogWriter exactly like the real
pipeline does (upsert_page -> insert_chunks -> activate_version), so the
catalog-adapter integration is exercised without Milvus or embeddings.
"""

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from services.catalog.models import Base, JobKind, VersionStatus
from services.catalog.repository import CatalogRepository
from services.crawler.jobs import (
    CrawlError,
    crawl_library_version_impl,
    devdocs_diff,
    devdocs_sync_impl,
    discover_page_urls,
    extract_title,
    refresh_stale_impl,
)
from services.crawler.polite_fetch import FetchError
from services.crawler.snapshots import InMemorySnapshotStore
from services.ingestion.models import Page


@pytest.fixture
def repo():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    return CatalogRepository(session)


class FakeFetcher:
    """Duck-typed PoliteFetcher over a URL -> (html, final_url) map."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    def fetch(self, url):
        self.calls.append(url)
        if url not in self.routes:
            raise FetchError(f"no route: {url}")
        resp = self.routes[url]
        if isinstance(resp, Exception):
            raise resp
        return resp


class FakeChunk:
    def __init__(self, page, i):
        self.page_id = page.content_hash
        self.chunk_order = i
        self.text = f"chunk {i} of {page.title}"
        self.token_count = 5
        self.headings = ["H"]


def make_ingest(calls):
    """Fake pipeline.ingest_pages that drives the catalog writer."""

    def fake_ingest(library_id, version, pages, source, **kwargs):
        calls.append(
            {
                "library_id": library_id,
                "version": version,
                "n_pages": len(pages),
                "source": source,
                "prev_version": kwargs.get("prev_version"),
            }
        )
        catalog = kwargs["catalog"]
        chunks = []
        for i, page in enumerate(pages):
            catalog.upsert_page(page, chunk_count=1)
            chunks.append(FakeChunk(page, i))
        catalog.insert_chunks(chunks)
        catalog.activate_version(library_id, version)
        return {"pages": len(pages), "source": source}

    return fake_ingest


SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://docs.example.com/3.1/install</loc></url>
  <url><loc>https://docs.example.com/3.1/api</loc></url>
  <url><loc>https://evil.example.org/steal</loc></url>
</urlset>"""

WEB_ROUTES = {
    "https://docs.example.com/versions.json": (
        '[{"version": "3.1", "url": "/3.1/"}, {"version": "3.0", "url": "/3.0/"}]',
        "https://docs.example.com/versions.json",
    ),
    "https://docs.example.com/3.1/sitemap.xml": (
        SITEMAP,
        "https://docs.example.com/3.1/sitemap.xml",
    ),
    "https://docs.example.com/3.1/install": (
        "<html><head><title>Install Guide</title></head><body>install it</body></html>",
        "https://docs.example.com/3.1/install",
    ),
    "https://docs.example.com/3.1/api": (
        "<html><head><title>API Reference</title></head><body>api docs</body></html>",
        "https://docs.example.com/3.1/api",
    ),
}


def test_crawl_web_happy_path(repo):
    lib = repo.create_library(name="example", docs_url="https://docs.example.com")
    fetcher = FakeFetcher(dict(WEB_ROUTES))
    snapshots = InMemorySnapshotStore()
    ingest_calls: list = []

    result = crawl_library_version_impl(
        str(lib.id),
        "3.1",
        repo=repo,
        fetcher=fetcher,
        snapshots=snapshots,
        ingest=make_ingest(ingest_calls),
        extract=lambda html_text, url: "# Doc\n\nbody",
        page_factory=Page,
        make_latest=True,
    )

    assert result["source"] == "crawl"
    assert result["strategy"] == "sphinx_versions_json"
    assert result["pages"] == 2
    # Out-of-scope sitemap entry never fetched (spec §6 scope control).
    assert "https://evil.example.org/steal" not in fetcher.calls
    # Ingest saw the right pages with source="crawl".
    assert ingest_calls[0]["source"] == "crawl"
    assert ingest_calls[0]["n_pages"] == 2
    # Snapshots stored, keys wired into catalog page rows.
    assert len(snapshots.objects) == 2
    version_row = repo.get_version_by_string(lib.id, "3.1")
    assert version_row.status == VersionStatus.READY.value
    assert version_row.is_latest is True
    pages = repo.list_pages(version_row.id)
    assert len(pages) == 2
    assert {p.title for p in pages} == {"Install Guide", "API Reference"}
    assert all(p.raw_snapshot_key in snapshots.objects for p in pages)
    assert repo.count_chunks(version_row.id) == 2
    # Job row succeeded.
    jobs = repo.list_jobs(library_id=lib.id)
    assert len(jobs) == 1
    assert jobs[0].status == "succeeded"
    assert jobs[0].kind == JobKind.CRAWL.value


def test_crawl_web_unknown_version_fails(repo):
    lib = repo.create_library(name="example", docs_url="https://docs.example.com")
    fetcher = FakeFetcher(dict(WEB_ROUTES))
    with pytest.raises(CrawlError, match="not in detected versions"):
        crawl_library_version_impl(
            str(lib.id),
            "9.9",
            repo=repo,
            fetcher=fetcher,
            snapshots=InMemorySnapshotStore(),
            ingest=make_ingest([]),
            extract=lambda h, u: "x",
            page_factory=Page,
        )
    version_row = repo.get_version_by_string(lib.id, "9.9")
    assert version_row.status == VersionStatus.FAILED.value
    jobs = repo.list_jobs(library_id=lib.id)
    assert jobs[0].status == "failed"
    assert jobs[0].error


def test_crawl_web_no_pages_fails(repo):
    lib = repo.create_library(name="example", docs_url="https://docs.example.com")
    routes = dict(WEB_ROUTES)
    routes["https://docs.example.com/3.1/sitemap.xml"] = (
        """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://docs.example.com/3.1/empty</loc></url>
        </urlset>""",
        "https://docs.example.com/3.1/sitemap.xml",
    )
    routes["https://docs.example.com/3.1/empty"] = (
        "<html><head><title>Empty</title></head><body></body></html>",
        "https://docs.example.com/3.1/empty",
    )
    fetcher = FakeFetcher(routes)
    with pytest.raises(CrawlError, match="no pages"):
        crawl_library_version_impl(
            str(lib.id),
            "3.1",
            repo=repo,
            fetcher=fetcher,
            snapshots=InMemorySnapshotStore(),
            ingest=make_ingest([]),
            extract=lambda h, u: "   ",  # nothing extractable
            page_factory=Page,
        )


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_git_run(repo_dir_files):
    """Fake git runner: ls-remote lists tags, clone materializes files."""

    def run(cmd, **kwargs):
        if cmd[1] == "ls-remote":
            return FakeProc(
                0,
                stdout=(
                    "a" * 40 + "\trefs/tags/v2.0.0\n"
                    + "b" * 40 + "\trefs/tags/v2.0.0^{}\n"
                    + "c" * 40 + "\trefs/tags/v1.0.0\n"
                ),
            )
        if cmd[1] == "clone":
            dest = Path(cmd[-1])
            for rel, text in repo_dir_files.items():
                p = dest / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
            return FakeProc(0, stdout="")
        if "rev-parse" in cmd:
            return FakeProc(0, stdout="d" * 40 + "\n")
        raise AssertionError(f"unexpected git command: {cmd}")

    return run


def test_crawl_repo_clone_path(repo):
    lib = repo.create_library(
        name="widget", repo_url="https://github.com/acme/widget"
    )
    run_git = make_git_run(
        {
            "README.md": "# Widget\n\nDoes things.",
            "docs/guide.md": "# Guide\n\nHow to.",
            "src/ignore.md": "# not docs",
        }
    )
    ingest_calls: list = []

    result = crawl_library_version_impl(
        str(lib.id),
        "2.0.0",  # matches tag "v2.0.0" via the v-prefix rule
        repo=repo,
        fetcher=FakeFetcher({}),
        snapshots=InMemorySnapshotStore(),
        ingest=make_ingest(ingest_calls),
        run_git=run_git,
    )

    assert result["source"] == "repoclone"
    assert result["tag"] == "v2.0.0"
    assert result["commit_sha"] == "d" * 40
    assert result["pages"] == 2  # README + docs/guide.md; src/ignore.md excluded
    assert ingest_calls[0]["source"] == "repoclone"
    version_row = repo.get_version_by_string(lib.id, "2.0.0")
    assert version_row.status == VersionStatus.READY.value
    pages = repo.list_pages(version_row.id)
    assert {p.title for p in pages} == {"Widget", "Guide"}
    jobs = repo.list_jobs(library_id=lib.id)
    assert jobs[0].kind == JobKind.REPOCLONE.value
    assert jobs[0].status == "succeeded"


def test_crawl_repo_no_matching_tag_fails(repo):
    lib = repo.create_library(
        name="widget", repo_url="https://github.com/acme/widget"
    )
    with pytest.raises(CrawlError, match="no git tag matching"):
        crawl_library_version_impl(
            str(lib.id),
            "9.9.9",
            repo=repo,
            fetcher=FakeFetcher({}),
            snapshots=InMemorySnapshotStore(),
            ingest=make_ingest([]),
            run_git=make_git_run({}),
        )
    version_row = repo.get_version_by_string(lib.id, "9.9.9")
    assert version_row.status == VersionStatus.FAILED.value


def test_refresh_stale_enqueues_each_stale_version(repo):
    lib = repo.create_library(name="example", docs_url="https://docs.example.com")
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)
    fresh = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    v_old, _ = repo.get_or_create_version(lib.id, "1.0", source="crawl")
    repo.mark_version_ready(v_old.id)
    repo.update_version_status(v_old.id, "ready", crawled_at=old)
    v_new, _ = repo.get_or_create_version(lib.id, "2.0", source="crawl")
    repo.mark_version_ready(v_new.id)
    repo.update_version_status(v_new.id, "ready", crawled_at=fresh)

    enqueued: list = []

    def fake_enqueue(library_id, version):
        enqueued.append((library_id, version))
        return "rq-123"

    result = refresh_stale_impl(repo=repo, enqueue=fake_enqueue)
    assert enqueued == [(str(lib.id), "1.0")]
    assert result == [
        {"library_id": str(lib.id), "version": "1.0", "rq_job_id": "rq-123"}
    ]


def test_devdocs_diff_skips_unchanged(repo):
    lib = repo.create_library(name="python")
    v, _ = repo.get_or_create_version(lib.id, "3.12", source="seed")
    repo.mark_version_ready(v.id)
    lib2 = repo.create_library(name="requests")
    v2, _ = repo.get_or_create_version(lib2.id, "2.28", source="seed")
    repo.mark_version_ready(v2.id)

    changes = devdocs_diff(
        {"python": "3.12", "requests": "2.31", "numpy": "2.0"}, repo
    )
    by_docset = {c["docset"]: c for c in changes}
    assert by_docset["python"]["action"] == "skip"
    assert by_docset["python"]["catalog_version"] == "3.12"
    assert by_docset["requests"]["action"] == "ingest"  # version changed
    assert by_docset["requests"]["catalog_version"] is None
    assert by_docset["numpy"]["action"] == "ingest"  # new docset


def test_devdocs_sync_impl_ingests_changed_docsets(repo):
    manifest = {"demo": "1.0", "unchanged": "2.0"}
    lib = repo.create_library(name="unchanged")
    v, _ = repo.get_or_create_version(lib.id, "2.0", source="seed")
    repo.mark_version_ready(v.id)

    importer_calls: list = []

    def fake_importer(docset_dir, *, library_id, version):
        importer_calls.append((docset_dir, library_id, version))
        return [
            Page(
                library_id=library_id,
                version=version,
                url="https://example.test/demo",
                title="Demo",
                markdown="# Demo\n\nbody",
                source="devdocs",
            )
        ]

    ingest_calls: list = []
    result = devdocs_sync_impl(
        manifest,
        repo=repo,
        ingest=make_ingest(ingest_calls),
        extractor=lambda docset: Path(f"/fake/{docset}"),
        importer=fake_importer,
    )

    actions = {c["docset"]: c["action"] for c in result["changes"]}
    assert actions == {"demo": "ingest", "unchanged": "skip"}
    assert len(result["results"]) == 1
    assert result["results"][0]["docset"] == "demo"
    assert importer_calls[0][0] == Path("/fake/demo")
    assert ingest_calls[0]["source"] == "seed"
    demo_lib = repo.get_library_by_name("demo")  # created on the fly
    demo_version = repo.get_version_by_string(demo_lib.id, "1.0")
    assert demo_version.status == VersionStatus.READY.value
    assert demo_version.source == "seed"


def test_discover_page_urls_falls_back_to_base():
    def fetch(url):
        raise FetchError(url)

    assert discover_page_urls("https://docs.example.com", fetch=fetch) == [
        "https://docs.example.com/"
    ]


def test_discover_page_urls_parses_sitemap_index():
    index = """<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://docs.example.com/s1.xml</loc></sitemap>
    </sitemapindex>"""
    child = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://docs.example.com/a</loc></url>
      <url><loc>https://docs.example.com/b</loc></url>
    </urlset>"""

    def fetch(url):
        if url.endswith("sitemap.xml"):
            return index, url
        if url.endswith("s1.xml"):
            return child, url
        raise FetchError(url)

    assert discover_page_urls("https://docs.example.com", fetch=fetch) == [
        "https://docs.example.com/a",
        "https://docs.example.com/b",
    ]


def test_extract_title():
    assert (
        extract_title("<html><head><title>  Hello &amp; World </title></head></html>")
        == "Hello & World"
    )
    assert extract_title("<html><body>no title</body></html>") == ""


# ---------------------------------------------------------------------------
# immutable revisions (spec §4)
# ---------------------------------------------------------------------------

from services.ingestion.models import content_chunk_id, sha256_text


class FakeVectorStore:
    """Ingestion VectorStore double: records chunk-ID deletions."""

    def __init__(self):
        self.deleted: list[str] = []

    def delete_chunks(self, chunk_ids):
        self.deleted.extend(chunk_ids)
        return len(chunk_ids)


def _crawl(lib_id, version, routes, extract_md, calls, store, **kw):
    fetcher = FakeFetcher(dict(routes))
    return crawl_library_version_impl(
        lib_id,
        version,
        repo=kw["repo"],
        fetcher=fetcher,
        snapshots=InMemorySnapshotStore(),
        ingest=make_ingest(calls),
        extract=lambda html_text, url: extract_md,
        page_factory=Page,
        make_latest=True,
        vector_store=store,
    )


def test_recrawl_inserts_new_revision_old_row_untouched(repo):
    """Spec §4: a re-crawl INSERTs revision N+1; the old row is immutable."""
    lib = repo.create_library(name="example", docs_url="https://docs.example.com")
    calls1, store1 = [], FakeVectorStore()
    r1 = _crawl(str(lib.id), "3.1", WEB_ROUTES, "# Doc\n\nbody", calls1, store1,
                repo=repo)
    assert r1["revision"] == 1
    row1 = repo.get_version_by_string(lib.id, "3.1")
    assert row1.revision == 1
    assert row1.status == VersionStatus.READY.value
    snapshot = (
        row1.id, row1.library_id, row1.version, row1.revision, row1.source,
        row1.status, row1.refresh_after_days, row1.commit_sha,
    )
    pages1 = [(p.url, p.title, p.content_hash) for p in repo.list_pages(row1.id)]

    # Re-crawl with changed content and one page removed.
    routes2 = dict(WEB_ROUTES)
    routes2["https://docs.example.com/3.1/sitemap.xml"] = (
        '<?xml version="1.0"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '  <url><loc>https://docs.example.com/3.1/install</loc></url>\n'
        "</urlset>",
        "https://docs.example.com/3.1/sitemap.xml",
    )
    routes2["https://docs.example.com/3.1/install"] = (
        "<html><head><title>Install Guide</title></head>"
        "<body>install it v2</body></html>",
        "https://docs.example.com/3.1/install",
    )
    calls2, store2 = [], FakeVectorStore()
    r2 = _crawl(str(lib.id), "3.1", routes2, "# Doc\n\nchanged body", calls2,
                store2, repo=repo)
    assert r2["revision"] == 2

    # New revision row; old row byte-identical.
    row2 = repo.get_version_by_string(lib.id, "3.1")
    assert row2.id != row1.id
    assert row2.revision == 2
    assert row2.status == VersionStatus.READY.value
    old = repo.get_version(row1.id)
    assert (
        old.id, old.library_id, old.version, old.revision, old.source,
        old.status, old.refresh_after_days, old.commit_sha,
    ) == snapshot
    assert [(p.url, p.title, p.content_hash)
            for p in repo.list_pages(row1.id)] == pages1
    # Retrieval serves the new revision.
    active, fallback = repo.resolve_version(lib.id, "3.1")
    assert active.id == row2.id and fallback is False
    # New revision got fresh page/chunk rows (no in-place aliasing).
    assert {p.url for p in repo.list_pages(row2.id)} == {
        "https://docs.example.com/3.1/install"}


def test_recrawl_prunes_orphan_chunk_ids(repo):
    """Orphan prune: chunk IDs in the old revision but not the new one
    are deleted from the vector store (content-derived IDs make unchanged
    chunks idempotent — they are NOT pruned)."""
    lib = repo.create_library(name="example", docs_url="https://docs.example.com")
    calls, store = [], FakeVectorStore()
    _crawl(str(lib.id), "3.1", WEB_ROUTES, "# Doc\n\nbody", calls, store,
           repo=repo)
    old_row = repo.get_active_version(lib.id, "3.1")

    # Re-crawl with identical content: nothing orphaned, no deletions.
    calls2, store2 = [], FakeVectorStore()
    _crawl(str(lib.id), "3.1", WEB_ROUTES, "# Doc\n\nbody", calls2, store2,
           repo=repo)
    assert store2.deleted == []

    # Re-crawl with changed content: old chunk IDs pruned.
    routes3 = dict(WEB_ROUTES)
    routes3["https://docs.example.com/3.1/install"] = (
        "<html><head><title>Install Guide</title></head>"
        "<body>totally different</body></html>",
        "https://docs.example.com/3.1/install",
    )
    calls3, store3 = [], FakeVectorStore()
    result = _crawl(str(lib.id), "3.1", routes3, "# Doc\n\nnew body", calls3,
                    store3, repo=repo)
    old_hash = sha256_text("# Doc\n\nbody")
    # NB: the fake ingest numbers chunks by page order, so the api page's
    # chunk text is "chunk 1 of API Reference", not "chunk 0 of ...".
    expected_orphans = {
        content_chunk_id("example", "3.1", old_hash, "chunk 0 of Install Guide"),
        content_chunk_id("example", "3.1", old_hash, "chunk 1 of API Reference"),
    }
    assert set(store3.deleted) == expected_orphans
    assert result["pruned_orphans"] == 2
    # Old revision row still intact in the catalog (prune is Milvus-only).
    assert repo.get_version(old_row.id).status == VersionStatus.READY.value


# ---------------------------------------------------------------------------
# P0 regressions: session commit on close & repo commit_sha persistence
# ---------------------------------------------------------------------------


def test_crawl_library_version_commits_before_closing_session(tmp_path, monkeypatch):
    """P0 Defect #1 regression: crawl_library_version() closes repo.session
    in its finally block; without an explicit commit, SQLAlchemy rolls back
    all catalog writes (version status, pages, chunks, job status)."""
    from services.crawler import jobs as jobs_mod

    db_path = tmp_path / "crawl_commit.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as seed_session:
        seed_repo = CatalogRepository(seed_session)
        lib = seed_repo.create_library(name="example", docs_url="https://docs.example.com")
        lib_uuid = lib.id
        lib_id = str(lib_uuid)
        seed_session.commit()

    monkeypatch.setattr(jobs_mod, "_open_repo", lambda: CatalogRepository(SessionLocal()))
    monkeypatch.setattr(jobs_mod, "PoliteFetcher", lambda: FakeFetcher(dict(WEB_ROUTES)))
    monkeypatch.setattr(jobs_mod, "RustFSSnapshotStore", lambda: InMemorySnapshotStore())
    monkeypatch.setattr(jobs_mod, "_real_ingest", make_ingest([]))
    monkeypatch.setattr(jobs_mod, "_real_extract", lambda html_text, url: "# Doc\n\nbody")
    monkeypatch.setattr(jobs_mod, "_real_vector_store", lambda: None)

    jobs_mod.crawl_library_version(lib_id, "3.1", make_latest=True)

    # Inspect from a fresh session after crawl_library_version closed its session.
    with SessionLocal() as verify_session:
        verify_repo = CatalogRepository(verify_session)
        v_row = verify_repo.get_version_by_string(lib_uuid, "3.1")
        assert v_row is not None, "version row rolled back on session close!"
        assert v_row.status == VersionStatus.READY.value
        assert v_row.is_latest is True
        assert len(verify_repo.list_pages(v_row.id)) == 2
        assert verify_repo.count_chunks(v_row.id) == 2
        jobs = verify_repo.list_jobs(library_id=lib_uuid)
        assert len(jobs) == 1 and jobs[0].status == "succeeded"
    engine.dispose()


def test_crawl_library_version_persists_failed_status_on_error(tmp_path, monkeypatch):
    """P0 Defect #1 regression: when crawl fails, FAILED status on version and
    job must be committed before repo.session.close()."""
    from services.crawler import jobs as jobs_mod

    db_path = tmp_path / "crawl_fail.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    with SessionLocal() as seed_session:
        seed_repo = CatalogRepository(seed_session)
        lib = seed_repo.create_library(name="example", docs_url="https://docs.example.com")
        lib_uuid = lib.id
        lib_id = str(lib_uuid)
        seed_session.commit()

    monkeypatch.setattr(jobs_mod, "_open_repo", lambda: CatalogRepository(SessionLocal()))
    monkeypatch.setattr(jobs_mod, "PoliteFetcher", lambda: FakeFetcher(dict(WEB_ROUTES)))
    monkeypatch.setattr(jobs_mod, "RustFSSnapshotStore", lambda: InMemorySnapshotStore())
    monkeypatch.setattr(jobs_mod, "_real_ingest", make_ingest([]))
    monkeypatch.setattr(jobs_mod, "_real_vector_store", lambda: None)

    with pytest.raises(CrawlError, match="not in detected versions"):
        jobs_mod.crawl_library_version(lib_id, "9.9")

    with SessionLocal() as verify_session:
        verify_repo = CatalogRepository(verify_session)
        v_row = verify_repo.get_version_by_string(lib_uuid, "9.9")
        assert v_row is not None, "failed version row rolled back on session close!"
        assert v_row.status == VersionStatus.FAILED.value
        jobs = verify_repo.list_jobs(library_id=lib_uuid)
        assert len(jobs) == 1 and jobs[0].status == "failed"
    engine.dispose()


def test_devdocs_sync_commits_before_closing_session(tmp_path, monkeypatch):
    """P0 Defect #1 regression: devdocs_sync() closes repo.session in its
    finally block; without an explicit commit, all synced libraries/versions/jobs
    roll back on session close."""
    import services.ingestion.importers as importers_mod
    from services.crawler import jobs as jobs_mod

    db_path = tmp_path / "devdocs_commit.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    monkeypatch.setattr(jobs_mod, "_open_repo", lambda: CatalogRepository(SessionLocal()))
    monkeypatch.setattr(jobs_mod, "_require_explicit_extractor", lambda docset: Path(f"/fake/{docset}"))
    monkeypatch.setattr(
        importers_mod,
        "import_devdocs_docset",
        lambda docset_dir, *, library_id, version: [
            Page(
                library_id=library_id,
                version=version,
                url="https://example.test/demo",
                title="Demo",
                markdown="# Demo\n\nbody",
                source="devdocs",
            )
        ],
    )
    monkeypatch.setattr(jobs_mod, "_real_ingest", make_ingest([]))
    monkeypatch.setattr(jobs_mod, "_real_vector_store", lambda: None)

    jobs_mod.devdocs_sync(manifest={"demo": "1.0"})

    with SessionLocal() as verify_session:
        verify_repo = CatalogRepository(verify_session)
        demo_lib = verify_repo.get_library_by_name("demo")
        assert demo_lib is not None, "devdocs_sync rolled back library creation on session close!"
        demo_ver = verify_repo.get_version_by_string(demo_lib.id, "1.0")
        assert demo_ver is not None and demo_ver.status == VersionStatus.READY.value
        jobs = verify_repo.list_jobs()
        assert len(jobs) == 1 and jobs[0].status == "succeeded"
    engine.dispose()


def test_crawl_repo_persists_commit_sha_on_version_row(repo):
    """P0 regression: _crawl_repo must persist commit_sha onto LibraryVersion."""
    lib = repo.create_library(name="widget", repo_url="https://github.com/acme/widget")
    run_git = make_git_run({"README.md": "# Widget\n\nDoes things."})
    crawl_library_version_impl(
        str(lib.id),
        "2.0.0",
        repo=repo,
        fetcher=FakeFetcher({}),
        snapshots=InMemorySnapshotStore(),
        ingest=make_ingest([]),
        run_git=run_git,
    )
    version_row = repo.get_version_by_string(lib.id, "2.0.0")
    assert version_row.commit_sha == "d" * 40
