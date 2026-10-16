"""RQ job functions for the crawler (spec §5.2, §5.1.1, §6, §9.3.1, §12).

RQ entry points (importable by dotted path; all arguments picklable):

- ``crawl_library_version(library_id, version, *, make_latest=False)``
  — §5.2 on-demand crawl / §9.3.1 repo-clone ingest for one
  (library, version). Repo-clone is preferred when the library record
  has a ``repo_url``; web crawl is the fallback.
- ``refresh_stale()`` — §12 sweep: enqueue an RQ crawl per stale version.
- ``devdocs_sync(manifest=None)`` — §5.1.1 monthly DevDocs image sync;
  with an explicit ``{docset: version}`` manifest it diffs + ingests
  directly; without one (production) it loads the approved seed manifest,
  re-resolves the image digest, pulls, stages, and diffs. ``enqueue_``
  ``devdocs_sync()`` puts one sync job on the ``sage-crawl`` queue for the
  worker — that is what the monthly scheduler calls.

Each entry point builds its real dependencies (catalog session from
``DATABASE_URL``, ``PoliteFetcher``, ``RustFSSnapshotStore``, the
ingestion pipeline) and delegates to a ``*_impl`` core with injectable
seams, so tests run fully offline.

Crawl flow (web)::

    detect version -> sitemap/page list -> polite_fetch each ->
    RustFS snapshot -> HTML->MD -> pipeline.ingest_pages(source="crawl") ->
    catalog mark ready

Crawl flow (repo)::

    git_tags -> match tag -> clone_at_tag -> find_doc_files ->
    import_markdown_files -> pipeline.ingest_pages(source="repoclone") ->
    catalog mark ready (commit SHA returned in the job report)
"""

from __future__ import annotations

import html as html_module
import logging
import os
import re
import tempfile
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .polite_fetch import PoliteFetcher
from .repo_clone import CloneError, clone_at_tag, find_doc_files, pages_from_clone
from .snapshots import RustFSSnapshotStore, snapshot_key
from .stale import needs_refresh  # noqa: F401  (re-exported policy helper)
from .version_detect import DetectionResult, detect, git_tags

log = logging.getLogger("sage.crawler.jobs")

CRAWL_QUEUE = "sage-crawl"
DEVDOCS_IMAGE = "ghcr.io/freecodecamp/devdocs:latest"
#: Ecosystem marker stamped on DevDocs-sourced library records so the
#: monthly sync can tell DevDocs docsets apart from other seed sources
#: (MDN, tldr) when detecting removed docsets.
DEVDOCS_ECOSYSTEM = "devdocs"
MAX_SITEMAP_URLS = 5000  # safety cap per (library, version) crawl


class CrawlError(Exception):
    """The crawl cannot proceed (no strategy, no tag, no pages, ...)."""


# ---------------------------------------------------------------------------
# dependency construction (production wiring)
# ---------------------------------------------------------------------------


def _open_repo():
    """Build a CatalogRepository from DATABASE_URL (one engine per job).

    Engines are created per job invocation, never cached at module level:
    RQ workers fork, and a fork-shared engine pool is unsafe.
    """
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set: refusing to guess the catalog.")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from services.catalog.repository import CatalogRepository

    engine = create_engine(db_url)
    session = sessionmaker(bind=engine)()
    return CatalogRepository(session)


def _redis():
    try:
        import redis
    except ImportError as exc:
        raise RuntimeError("redis is not installed: the RQ worker needs it.") from exc
    url = os.environ.get("REDIS_URL", "")
    if not url:
        raise RuntimeError("REDIS_URL is not set: refusing to guess the queue.")
    return redis.from_url(url)


def _real_ingest(library_id, version, pages, source, **kwargs):
    from services.ingestion.pipeline import ingest_pages

    return ingest_pages(library_id, version, pages, source, **kwargs)


def _real_extract(html_text: str, url: str) -> str:
    from services.ingestion.html_to_md import html_to_markdown

    return html_to_markdown(html_text, url)


def _real_page_factory(**kwargs):
    from services.ingestion.models import Page

    return Page(**kwargs)


def _real_vector_store():
    """Lazy Milvus vector store for the orphan prune (production wiring).

    Kept lazy like ``_real_ingest``: the RQ entry points build it, tests
    inject a fake (or None to skip pruning).
    """
    from services.ingestion.pipeline import MilvusVectorStore

    return MilvusVectorStore()


def _begin_version_ingest(repo, lib, version: str, *, source: str):
    """Spec §4 immutability: re-crawls INSERT a new revision row.

    Returns ``(version_row, prev_active_id)``. ``prev_active_id`` is the
    previously active (newest READY) revision, or ``None`` on first
    crawl. Existing rows are never mutated.
    """
    prev_active = repo.get_active_version(lib.id, version)
    if prev_active is not None:
        row = repo.create_version_revision(lib.id, version, source=source)
        return row, prev_active.id
    row, _ = repo.get_or_create_version(lib.id, version, source=source)
    return row, None


def _chunk_ids_for_revision(repo, library_slug: str, version: str,
                            version_row_id: uuid.UUID) -> set[str]:
    """Content-derived Milvus chunk IDs for every chunk of a revision row.

    Must use the exact inputs the chunker saw
    (``content_chunk_id(library_id, version, page_hash, text)``) so the
    IDs match what ``ingest_pages`` upserted.
    """
    from services.ingestion.models import content_chunk_id

    ids: set[str] = set()
    offset = 0
    while True:
        pages = repo.list_pages(version_row_id, limit=500, offset=offset)
        if not pages:
            break
        for page in pages:
            page_hash = page.content_hash or ""
            for chunk in repo.list_chunks(page.id):
                ids.add(content_chunk_id(library_slug, version, page_hash, chunk.text))
        offset += len(pages)
    return ids


def _prune_superseded_chunks(repo, library_slug: str, version: str,
                             old_version_id: uuid.UUID,
                             new_version_id: uuid.UUID,
                             vector_store) -> int:
    """Delete Milvus chunk records orphaned by a re-crawl (spec §4).

    Chunk IDs are content-derived, so unchanged chunks upsert idempotently
    and need no handling; only IDs present in the old revision but absent
    from the new one are deleted. Runs after the new revision is READY,
    so there is no serving gap (stale-while-revalidate, §6). Returns the
    orphan count (0 when ``vector_store`` is None, e.g. in tests).
    """
    if vector_store is None:
        return 0
    old_ids = _chunk_ids_for_revision(repo, library_slug, version, old_version_id)
    new_ids = _chunk_ids_for_revision(repo, library_slug, version, new_version_id)
    orphans = sorted(old_ids - new_ids)
    if not orphans:
        return 0
    deleted = vector_store.delete_chunks(orphans)
    log.info("pruned %d orphan chunk(s) superseded by %s@%s re-crawl",
             deleted, library_slug, version)
    return deleted


# ---------------------------------------------------------------------------
# catalog adapter: ingestion CatalogWriter -> CatalogRepository
# ---------------------------------------------------------------------------


class RepoCatalogWriter:
    """Adapt the ingestion pipeline's ``CatalogWriter`` protocol onto the
    real ``CatalogRepository`` (spec §4 page/chunk rows).

    The pipeline's ``Page.page_id`` for chunks is the page's
    ``content_hash`` (see ``services.ingestion.models.Chunk``), so chunk
    rows are routed to catalog pages via a content-hash map built in
    ``upsert_page``.
    """

    def __init__(self, repo, version_id: uuid.UUID, *, make_latest: bool = False) -> None:
        self._repo = repo
        self._version_id = version_id
        self._make_latest = make_latest
        self._page_ids: dict[str, uuid.UUID] = {}

    def page_hashes(self, library_id: str, version: str) -> set[str]:
        # ``library_id`` is the canonical slug (``Library.name``); the
        # UUID primary keys stay inside Postgres. Dedup reads the active
        # (newest READY) revision of the previous version.
        lib = self._repo.resolve_library(library_id)
        if lib is None:
            return set()
        row = self._repo.get_active_version(lib.id, version)
        if row is None:
            return set()
        return {
            p.content_hash
            for p in self._repo.list_pages(row.id)
            if p.content_hash
        }

    def upsert_page(self, page, *, chunk_count: int) -> None:
        key = (page.metadata or {}).get("raw_snapshot_key")
        row = self._repo.record_page(
            self._version_id,
            url=page.url or None,
            title=page.title,
            content_hash=page.content_hash,
            raw_snapshot_key=key,
        )
        self._page_ids[page.content_hash] = row.id

    def insert_chunks(self, chunks: list) -> int:
        by_page: dict[str, list] = {}
        for chunk in chunks:
            by_page.setdefault(chunk.page_id, []).append(chunk)
        total = 0
        for page_hash, group in by_page.items():
            page_row_id = self._page_ids.get(page_hash)
            if page_row_id is None:
                raise RuntimeError(
                    f"chunk for unknown page hash {page_hash}: upsert_page "
                    "was not called first (pipeline contract violated)"
                )
            self._repo.record_chunks(
                page_row_id,
                [
                    {
                        "ordinal": c.chunk_order,
                        "text": c.text,
                        "token_count": c.token_count,
                        "headings": list(c.headings or []),
                    }
                    for c in group
                ],
            )
            total += len(group)
        return total

    def alias_page(self, library_id: str, from_version: str, to_version: str, page) -> None:
        # §6 dedup: unchanged page — record the row (same content hash,
        # no snapshot; nothing was re-fetched) but never re-chunk it.
        row = self._repo.record_page(
            self._version_id,
            url=page.url or None,
            title=page.title,
            content_hash=page.content_hash,
            raw_snapshot_key=None,
        )
        self._page_ids[page.content_hash] = row.id

    def activate_version(self, library_id: str, version: str) -> None:
        self._repo.mark_version_ready(self._version_id, make_latest=self._make_latest)


# ---------------------------------------------------------------------------
# enqueue helpers
# ---------------------------------------------------------------------------


def enqueue_crawl(
    library_id: str,
    version: str,
    *,
    make_latest: bool = False,
    connection=None,
    job_timeout: int = 7200,
) -> str:
    """Enqueue one (library, version) crawl on the ``sage-crawl`` queue."""
    from rq import Queue

    conn = connection or _redis()
    queue = Queue(CRAWL_QUEUE, connection=conn)
    job = queue.enqueue(
        "services.crawler.jobs.crawl_library_version",
        library_id,
        version,
        make_latest=make_latest,
        job_timeout=job_timeout,
    )
    log.info("enqueued crawl job %s for %s@%s", job.id, library_id, version)
    return job.id


# ---------------------------------------------------------------------------
# sitemap / page-list discovery
# ---------------------------------------------------------------------------


def _in_scope(url: str, base_url: str) -> bool:
    """Spec §6 scope control: same origin + base path prefix."""
    try:
        u, b = urlsplit(url), urlsplit(base_url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https"):
        return False
    if (u.scheme, u.netloc.lower()) != (b.scheme, b.netloc.lower()):
        return False
    base_path = b.path.rstrip("/") + "/"
    return u.path.startswith(base_path) or u.path.rstrip("/") == b.path.rstrip("/")


def _parse_sitemap_urls(xml_text: str) -> tuple[list[str], list[str]]:
    """Return (page_urls, child_sitemap_urls) from a sitemap document."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], []
    tag = root.tag
    ns = ""
    if tag.startswith("{"):
        ns = tag.split("}")[0] + "}"
    page_urls: list[str] = []
    child_sitemaps: list[str] = []
    if root.tag.endswith("sitemapindex"):
        for sm in root.findall(f"{ns}sitemap"):
            loc = sm.find(f"{ns}loc")
            if loc is not None and loc.text:
                child_sitemaps.append(loc.text.strip())
    else:
        for url_el in root.findall(f"{ns}url"):
            loc = url_el.find(f"{ns}loc")
            if loc is not None and loc.text:
                page_urls.append(loc.text.strip())
    return page_urls, child_sitemaps


def discover_page_urls(base_url: str, *, fetch) -> list[str]:
    """Find the crawlable page list for a docs base URL.

    Fetches ``<base>/sitemap.xml`` (recursing one level into a
    sitemap index), filters to the §6 scope, and falls back to just the
    base URL when no sitemap exists. ``fetch`` behaves like
    ``PoliteFetcher.fetch``.
    """
    base = base_url.rstrip("/")
    sitemap_urls: list[str] = []
    for candidate in (f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"):
        try:
            xml_text, _ = fetch(candidate)
        except Exception as exc:
            log.debug("sitemap fetch failed for %s: %s", candidate, exc)
            continue
        pages, children = _parse_sitemap_urls(xml_text)
        sitemap_urls.extend(pages)
        for child in children:  # one level of index recursion
            try:
                child_text, _ = fetch(child)
            except Exception as exc:
                log.debug("child sitemap fetch failed for %s: %s", child, exc)
                continue
            child_pages, _ = _parse_sitemap_urls(child_text)
            sitemap_urls.extend(child_pages)
        if sitemap_urls:
            break
    if not sitemap_urls:
        log.info("no sitemap at %s: crawling base URL only", base)
        return [base + "/"] if not base.endswith("/") else [base]
    seen: list[str] = []
    for url in sitemap_urls:
        if url not in seen and _in_scope(url, base):
            seen.append(url)
        if len(seen) >= MAX_SITEMAP_URLS:
            log.warning("sitemap capped at %d URLs for %s", MAX_SITEMAP_URLS, base)
            break
    if not seen:
        return [base + "/"]
    log.info("discovered %d in-scope pages under %s", len(seen), base)
    return seen


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def extract_title(html_text: str) -> str:
    """Best-effort ``<title>`` extraction for catalog page rows."""
    m = _TITLE_RE.search(html_text or "")
    if not m:
        return ""
    title = re.sub(r"\s+", " ", html_module.unescape(m.group(1))).strip()
    return title[:500]


# ---------------------------------------------------------------------------
# crawl_library_version
# ---------------------------------------------------------------------------


def _match_tag(tags: list[str] | None, version: str) -> str | None:
    """Match a requested version string to a git tag."""
    if not tags:
        return None
    if version in tags:
        return version
    for candidate in (f"v{version}", version.lstrip("v")):
        if candidate in tags:
            return candidate
    return None


def _prev_ready_version(repo, library_id: uuid.UUID, version: str) -> str | None:
    latest = repo.get_latest_version(library_id)
    if latest is not None and latest.version != version and latest.status == "ready":
        return latest.version
    return None


def crawl_library_version_impl(
    library_id: str,
    version: str,
    *,
    repo,
    fetcher: PoliteFetcher,
    snapshots,
    ingest,
    run_git=None,
    extract=None,
    page_factory=None,
    make_latest: bool = False,
    vector_store=None,
) -> dict:
    """Core crawl logic with injectable seams (see module docstring).

    ``vector_store`` (ingestion ``VectorStore``) is used after a
    successful re-crawl to prune Milvus chunk records orphaned by the new
    revision; pass ``None`` to skip pruning (tests).
    """
    from services.catalog.models import JobKind, VersionStatus

    lib = repo.get_library(uuid.UUID(library_id))
    use_repo = bool(lib.repo_url)
    source = "repoclone" if use_repo else "crawl"
    # Spec §4: the version row is immutable — a re-crawl of an already
    # indexed (library, version) INSERTs a new revision row; the old row
    # is never updated in place.
    version_row, prev_active_id = _begin_version_ingest(
        repo, lib, version, source=source
    )
    kind = JobKind.REPOCLONE if use_repo else JobKind.CRAWL
    job = repo.enqueue_job(kind=kind, library_id=lib.id, version=version)
    repo.update_job_status(job.id, "running")
    log.info("crawl start: %s@%s via %s (revision %d)",
             lib.name, version, source, version_row.revision)
    try:
        if use_repo:
            result = _crawl_repo(
                repo, lib, version_row, version, run_git=run_git,
                ingest=ingest, make_latest=make_latest,
                vector_store=vector_store,
            )
        else:
            result = _crawl_web(
                repo, lib, version_row, version, fetcher=fetcher,
                snapshots=snapshots, ingest=ingest, run_git=run_git,
                extract=extract or _real_extract,
                page_factory=page_factory or _real_page_factory,
                make_latest=make_latest, vector_store=vector_store,
            )
    except Exception as exc:
        repo.update_version_status(version_row.id, VersionStatus.FAILED)
        repo.update_job_status(job.id, "failed", error=str(exc)[:2000])
        log.error("crawl failed: %s@%s: %s", lib.name, version, exc)
        raise
    if prev_active_id is not None:
        result["pruned_orphans"] = _prune_superseded_chunks(
            repo, lib.name, version, prev_active_id, version_row.id,
            vector_store,
        )
    repo.update_job_status(job.id, "succeeded")
    return {"library": lib.name, "version": version, "source": source,
            "revision": version_row.revision, **result}


def _crawl_repo(repo, lib, version_row, version, *, run_git, ingest,
              make_latest, vector_store=None) -> dict:
    """§9.3.1 path: tags are the version; Markdown needs no extraction."""
    from services.catalog.models import VersionStatus

    repo.update_version_status(version_row.id, VersionStatus.CLONING)
    tags = git_tags(lib.repo_url, run=run_git)
    tag = _match_tag(tags, version)
    if tag is None:
        raise CrawlError(
            f"no git tag matching version {version!r} in {lib.repo_url} "
            f"(tags seen: {(tags or [])[:10]})"
        )
    prev_version = _prev_ready_version(repo, lib.id, version)
    with tempfile.TemporaryDirectory(prefix="sage-clone-") as tmp:
        checkout = Path(tmp) / "repo"
        sha = clone_at_tag(lib.repo_url, tag, checkout, run=run_git)
        pages = pages_from_clone(
            checkout, library_id=lib.name, version=version, source="repoclone",
            repo_url=lib.repo_url, commit_sha=sha,
        )
    writer = RepoCatalogWriter(repo, version_row.id, make_latest=make_latest)
    # Spec §9.3.1: the clone is reproducible from the tag; the commit SHA
    # is the snapshot. (A RustFS tarball of the checkout is future work —
    # see README gaps.)
    # NOTE: library_id here is the canonical slug (Library.name), matching
    # the retrieval predicate and the ingestion CatalogWriter contract —
    # the UUID primary keys stay inside Postgres.
    report = ingest(
        lib.name, version, pages, "repoclone",
        catalog=writer, prev_version=prev_version, vector_store=vector_store,
    )
    return {
        "tag": tag,
        "commit_sha": sha,
        "pages": len(pages),
        "ingest": report,
    }


def _resolve_docs_base(lib, version, *, fetcher, run_git) -> tuple[str, DetectionResult | None]:
    """Pick the docs base URL for the requested version (§6 strategies)."""
    if not lib.docs_url:
        raise CrawlError(
            f"library {lib.name!r} has no docs_url and no repo_url: "
            "cannot crawl"
        )
    detected = detect(lib, fetch=fetcher.fetch, run=run_git)
    if detected is None:
        log.info("no version strategy fired for %s: using docs_url", lib.name)
        return lib.docs_url, None
    if detected.versions:
        if version in detected.versions:
            return detected.versions[version], detected
        raise CrawlError(
            f"version {version!r} not in detected versions for {lib.name!r} "
            f"(strategy={detected.strategy}; known: {sorted(detected.versions)[:10]})"
        )
    if detected.resolved_url:
        # latest_redirect only resolves the newest docs; use it as-is and
        # let provenance record the actual URL served.
        return detected.resolved_url, detected
    return lib.docs_url, detected


def _crawl_web(
    repo, lib, version_row, version, *,
    fetcher, snapshots, ingest, run_git, extract, page_factory, make_latest,
    vector_store=None,
) -> dict:
    """§5.2 path: sitemap -> polite fetch -> snapshots -> ingest."""
    from services.catalog.models import VersionStatus

    repo.update_version_status(version_row.id, VersionStatus.CRAWLING)
    base_url, detected = _resolve_docs_base(lib, version, fetcher=fetcher, run_git=run_git)
    page_urls = discover_page_urls(base_url, fetch=fetcher.fetch)
    pages: list = []
    for url in page_urls:
        html_text, final_url = fetcher.fetch(url)
        key = snapshots.put_html(snapshot_key(lib.name, version, final_url), html_text)
        markdown = extract(html_text, final_url)
        if not markdown.strip():
            log.debug("no extractable content at %s: skipping", final_url)
            continue
        pages.append(
            page_factory(
                # NOTE: canonical slug (Library.name), not the UUID — must
                # match the retrieval predicate and chunk-ID derivation.
                library_id=lib.name,
                version=version,
                url=final_url,
                title=extract_title(html_text) or final_url,
                markdown=markdown,
                source="crawl",
                metadata={"raw_snapshot_key": key},
            )
        )
    if not pages:
        raise CrawlError(f"crawl of {lib.name}@{version} produced no pages")
    repo.update_version_status(version_row.id, VersionStatus.PARSING)
    prev_version = _prev_ready_version(repo, lib.id, version)
    writer = RepoCatalogWriter(repo, version_row.id, make_latest=make_latest)
    report = ingest(
        lib.name, version, pages, "crawl",
        catalog=writer, prev_version=prev_version, vector_store=vector_store,
    )
    return {
        "strategy": detected.strategy if detected else "docs_url",
        "page_urls_discovered": len(page_urls),
        "pages": len(pages),
        "ingest": report,
    }


def crawl_library_version(
    library_id: str, version: str, *, make_latest: bool = False
) -> dict:
    """RQ entry point: crawl one (library, version) with real dependencies."""
    repo = _open_repo()
    try:
        return crawl_library_version_impl(
            library_id,
            version,
            repo=repo,
            fetcher=PoliteFetcher(),
            snapshots=RustFSSnapshotStore(),
            ingest=_real_ingest,
            make_latest=make_latest,
            vector_store=_real_vector_store(),
        )
    finally:
        repo.session.close()


# ---------------------------------------------------------------------------
# refresh_stale (§12 sweep)
# ---------------------------------------------------------------------------


def refresh_stale_impl(*, repo, enqueue) -> list[dict]:
    """Enqueue one RQ crawl per stale version. Returns what was enqueued."""
    stale = repo.list_stale_versions()
    enqueued: list[dict] = []
    for version_row in stale:
        library_id = str(version_row.library_id)
        rq_job_id = enqueue(library_id, version_row.version)
        enqueued.append(
            {
                "library_id": library_id,
                "version": version_row.version,
                "rq_job_id": rq_job_id,
            }
        )
    log.info("refresh sweep: %d stale version(s) enqueued", len(enqueued))
    return enqueued


def refresh_stale() -> list[dict]:
    """RQ entry point: §12 stale-while-revalidate sweep."""
    repo = _open_repo()
    try:
        return refresh_stale_impl(repo=repo, enqueue=enqueue_crawl)
    finally:
        repo.session.close()


# ---------------------------------------------------------------------------
# devdocs_sync (§5.1.1 monthly sync)
# ---------------------------------------------------------------------------
#
# Production flow (manifest=None): load the approved seed manifest
# (services/artifacts/manifests/seed-corpus.yaml, APPROVAL-2026-101) for
# the digest-pinned image reference → re-resolve :latest's digest via
# registry metadata → docker pull the resolved digest → stage the docset
# tree out of the image → build {docset: version} → diff → ingest.
# Manifest plumbing lives in .devdocs_manifest (stdlib-only); the diff
# and ingest cores below stay injectable for offline tests.


def _pull_devdocs_image(image: str = DEVDOCS_IMAGE) -> None:
    """PRODUCTION ONLY — shell out to ``docker pull``.

    Clearly-marked subprocess call: tests must NEVER invoke this (it needs
    docker and the network). ``devdocs_sync`` only reaches it on the
    production path, after the manifest is loaded and the digest
    re-resolved — the pull itself is the one explicitly approved network
    fetch in this design (spec §5.1.1).
    """
    import subprocess

    log.info("pulling DevDocs image %s", image)
    subprocess.run(["docker", "pull", image], check=True, timeout=1800)


@contextmanager
def _staged_production_manifest():
    """Build the production ``{docset: version}`` manifest from the image.

    Yields ``(manifest, docset_dirs, image_info)`` with the staging
    directory alive; cleans it up on exit. Raises
    :class:`DevDocsManifestError` (via ``devdocs_manifest``) with an
    actionable message on any failure — missing manifest file, bad digest,
    registry unreachable, docker unavailable.
    """
    from .devdocs_manifest import (
        docset_manifest_from_stage,
        load_devdocs_image_ref,
        resolve_latest_digest,
        stage_image_docsets,
    )

    ref = load_devdocs_image_ref()
    latest_digest = resolve_latest_digest(ref.repo)
    if latest_digest != ref.digest:
        log.info(
            "devdocs sync: image moved since approval (pinned sha256:%s -> "
            "sha256:%s); pulling the resolved digest",
            ref.digest,
            latest_digest,
        )
    image = "%s@sha256:%s" % (ref.repo, latest_digest)
    _pull_devdocs_image(image)
    with tempfile.TemporaryDirectory(prefix="devdocs-sync-") as staging:
        docsets_root = stage_image_docsets(image, staging)
        manifest = docset_manifest_from_stage(docsets_root)
        docset_dirs = {name: docsets_root / name for name in manifest}
        image_info = {
            "image": image,
            "resolved_digest": latest_digest,
            "pinned_digest": ref.digest,
            "manifest_path": str(ref.manifest_path),
            "approval_record": ref.approval_record,
        }
        yield manifest, docset_dirs, image_info


def _served_version_string(repo, lib) -> str | None:
    """Version string currently served for a library (informational).

    The ``is_latest`` READY row when one is flagged (what
    ``get-library-docs`` falls back to); otherwise the newest READY row.
    """
    from services.catalog.models import VersionStatus

    latest = repo.get_latest_version(lib.id)
    if latest is not None:
        return latest.version
    rows = repo.list_versions(lib.id, status=VersionStatus.READY, limit=1)
    return rows[0].version if rows else None


def devdocs_diff(manifest: dict[str, str], repo) -> list[dict]:
    """Diff a ``{docset: version}`` manifest against the catalog (§5.1.1).

    Pure logic, no docker, no network: for each docset, the catalog's
    READY row for the manifest version decides. Returns one entry per
    manifest docset plus one per removed docset, each with:

    - ``classification``: ``"unchanged"`` (READY row for the manifest
      version exists — no re-chunk, no re-embed), ``"changed"`` (version
      differs), ``"added"`` (no library row yet), or ``"removed"`` (the
      catalog holds a DevDocs-sourced library absent from the manifest).
    - ``action``: ``"skip"`` for unchanged/removed, ``"ingest"`` for
      changed/added.

    Removed docsets are never deleted: the catalog is append-only (§4),
    so they stay searchable until explicitly pruned. Removal is detected
    via the ``devdocs`` ecosystem marker that ``devdocs_sync_impl``
    stamps on every library it creates or ingests.
    """
    changes: list[dict] = []
    for docset in sorted(manifest):
        manifest_version = manifest[docset]
        catalog_version: str | None = None
        lib = repo.get_library_by_name(docset)
        if lib is not None:
            # Active (newest READY) revision decides: a re-ingest is only
            # needed when the manifest version has no searchable revision.
            row = repo.get_active_version(lib.id, manifest_version)
            if row is not None:
                catalog_version = manifest_version
        if catalog_version == manifest_version:
            classification, action = "unchanged", "skip"
        elif lib is None:
            classification, action = "added", "ingest"
        else:
            classification, action = "changed", "ingest"
        changes.append(
            {
                "docset": docset,
                "manifest_version": manifest_version,
                "catalog_version": catalog_version,
                "classification": classification,
                "action": action,
            }
        )
    # Removed: DevDocs-sourced catalog libraries absent from the manifest.
    # Paginated: a full seed corpus is dozens of libraries, but never
    # assume it fits one page.
    offset = 0
    while True:
        libs = repo.list_libraries(
            ecosystem=DEVDOCS_ECOSYSTEM, limit=500, offset=offset
        )
        if not libs:
            break
        for lib in libs:
            if lib.name in manifest:
                continue
            changes.append(
                {
                    "docset": lib.name,
                    "manifest_version": None,
                    "catalog_version": _served_version_string(repo, lib),
                    "classification": "removed",
                    "action": "skip",
                }
            )
            log.info(
                "devdocs sync: docset %r removed upstream; keeping catalog "
                "rows (append-only, §4)",
                lib.name,
            )
        offset += len(libs)
    return changes


def devdocs_sync_impl(manifest, *, repo, ingest, extractor, importer=None,
                      vector_store=None) -> dict:
    """Sync changed DevDocs docsets (diff already decided by ``devdocs_diff``).

    ``importer`` defaults to the real ``import_devdocs_docset``; tests
    inject a fake (the real one needs trafilatura/readability, which are
    Docker-image dependencies, not test dependencies). ``vector_store``
    prunes Milvus orphans after re-ingests (None skips, e.g. in tests).
    """
    from services.catalog.models import VersionStatus

    if importer is None:
        from services.ingestion.importers import import_devdocs_docset as importer

    changes = devdocs_diff(manifest, repo)
    results: list[dict] = []
    for change in changes:
        if change["action"] == "skip":
            continue
        docset, mver = change["docset"], change["manifest_version"]
        lib = repo.get_library_by_name(docset)
        if lib is None:
            lib = repo.create_library(name=docset, ecosystem=DEVDOCS_ECOSYSTEM)
        elif lib.ecosystem != DEVDOCS_ECOSYSTEM:
            # Backfill the marker on libraries created before it existed,
            # so removed-docset detection (devdocs_diff) sees them.
            repo.update_library(lib.id, ecosystem=DEVDOCS_ECOSYSTEM)
        # Spec §4: re-ingest INSERTs a new revision; never mutates the row.
        version_row, prev_active_id = _begin_version_ingest(
            repo, lib, mver, source="seed"
        )
        repo.update_version_status(version_row.id, VersionStatus.PARSING)
        docset_dir = extractor(docset)
        pages = importer(
            docset_dir, library_id=lib.name, version=mver
        )
        writer = RepoCatalogWriter(repo, version_row.id, make_latest=True)
        report = ingest(lib.name, mver, pages, "seed", catalog=writer,
                        vector_store=vector_store)
        if prev_active_id is not None:
            pruned = _prune_superseded_chunks(
                repo, lib.name, mver, prev_active_id, version_row.id,
                vector_store,
            )
            report["pruned_orphans"] = pruned
        results.append({"docset": docset, "version": mver, "pages": len(pages), "ingest": report})
    return {"changes": changes, "results": results}


def devdocs_sync(manifest: dict[str, str] | None = None) -> dict:
    """RQ entry point: monthly DevDocs sync (§5.1.1, §17.6).

    With an explicit ``{docset: version}`` manifest, runs the diff +
    ingest path (tests and manual runs pass ``extractor`` through
    ``devdocs_sync_impl`` directly). Without one — the production path —
    loads the approved seed manifest, re-resolves the image digest via
    registry metadata, pulls the resolved digest, stages the docset tree
    out of the image, and syncs from that. A ``crawl_job`` row of kind
    ``devdocs_sync`` records the run durably; any failure marks it
    ``failed`` with the error and re-raises — never silently pretending
    to have synced.
    """
    from services.catalog.models import JobKind, JobStatus

    if manifest is None:
        # Fail fast on manifest problems (missing file, bad digest) with
        # the clear path-pointing error — before touching the database.
        from .devdocs_manifest import load_devdocs_image_ref

        load_devdocs_image_ref()

    repo = _open_repo()
    job_row = None
    try:
        job_row = repo.enqueue_job(kind=JobKind.DEVDOCS_SYNC)
        repo.update_job_status(job_row.id, JobStatus.RUNNING)
        if manifest is None:
            with _staged_production_manifest() as (prod_manifest, docset_dirs, image_info):
                result = devdocs_sync_impl(
                    prod_manifest,
                    repo=repo,
                    ingest=_real_ingest,
                    extractor=docset_dirs.__getitem__,
                    vector_store=_real_vector_store(),
                )
                result["image"] = image_info
        else:
            result = devdocs_sync_impl(
                manifest,
                repo=repo,
                ingest=_real_ingest,
                extractor=_require_explicit_extractor,
                vector_store=_real_vector_store(),
            )
        repo.update_job_status(job_row.id, JobStatus.SUCCEEDED)
        result["crawl_job_id"] = str(job_row.id)
        return result
    except Exception as exc:
        if job_row is not None:
            repo.update_job_status(
                job_row.id, JobStatus.FAILED,
                error="%s: %s" % (type(exc).__name__, exc),
            )
        raise
    finally:
        repo.session.close()


def _require_explicit_extractor(docset: str) -> Path:
    """Extractor guard for ``devdocs_sync(manifest={...})`` without one.

    The production path (``manifest=None``) stages the image itself; an
    explicit manifest needs an explicit extractor — fail closed with a
    clear message instead of a bare NotImplementedError.
    """
    from .devdocs_manifest import DEVDOCS_MANIFEST_ENV, DevDocsManifestError

    raise DevDocsManifestError(
        "devdocs_sync() called with an explicit manifest but no extractor: "
        "pass extractor= to devdocs_sync_impl(), or call devdocs_sync() "
        "with manifest=None so the approved seed manifest "
        "(%s) drives the image pull + staging" % DEVDOCS_MANIFEST_ENV
    )


def enqueue_devdocs_sync(
    *,
    connection=None,
    job_timeout: int = 6 * 3600,
) -> str:
    """Enqueue one monthly DevDocs sync on the ``sage-crawl`` queue.

    Mirrors :func:`enqueue_crawl`: the worker runs
    ``services.crawler.jobs.devdocs_sync`` (manifest=None → production
    image path). The monthly scheduler calls this — it never pulls or
    ingests anything itself. Generous timeout: a pull plus a multi-docset
    ingest is hours of work, not minutes.
    """
    from rq import Queue

    conn = connection or _redis()
    queue = Queue(CRAWL_QUEUE, connection=conn)
    job = queue.enqueue(
        "services.crawler.jobs.devdocs_sync",
        job_timeout=job_timeout,
    )
    log.info("enqueued devdocs_sync job %s", job.id)
    return job.id
