"""RQ import job: immutable raw files -> SAGE's versioned ingestion pipeline."""

from __future__ import annotations

import hashlib
import logging
from pathlib import PurePosixPath

from .core import MAX_ARCHIVE_BYTES, pages_from_documents, read_documents, validate_identity
from .storage import ImportStorage

log = logging.getLogger(__name__)


def process_import_impl(
    library: str,
    version: str,
    upload_id: str,
    archive: bytes,
    *,
    repo,
    storage,
    ingest,
    vector_store=None,
) -> dict:
    """Testable job core. Never mutate an existing READY revision in place."""
    from services.catalog.models import JobKind, VersionStatus
    from services.crawler.jobs import RepoCatalogWriter, _prune_superseded_chunks

    library, version = validate_identity(library, version)
    files = read_documents(archive)
    lib = repo.get_library_by_name(library)
    if lib is None:
        lib = repo.create_library(name=library, resolver_notes="Admin-managed uploaded documents")
    previous = repo.get_active_version(lib.id, version)
    row = repo.create_version_revision(lib.id, version, source="manual")
    job = repo.enqueue_job(kind=JobKind.MANUAL_IMPORT, library_id=lib.id, version=version)
    repo.update_job_status(job.id, "running")
    repo.session.commit()

    try:
        snapshot_keys = {}
        for path, raw in files:
            suffix = PurePosixPath(path).suffix.lower()
            digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
            key = f"manual-imports/{upload_id}/files/{digest}{suffix}"
            mime = (
                "text/html; charset=utf-8"
                if suffix in {".html", ".htm"}
                else "text/markdown; charset=utf-8"
            )
            snapshot_keys[path] = storage.put(key, raw, mime)
        pages = pages_from_documents(files, library, version, snapshot_keys=snapshot_keys)
        repo.update_version_status(row.id, VersionStatus.PARSING)
        writer = RepoCatalogWriter(repo, row.id, make_latest=True)
        report = ingest(
            library,
            version,
            pages,
            "manual",
            catalog=writer,
            vector_store=vector_store,
        )
        if previous is not None:
            report["pruned_orphans"] = _prune_superseded_chunks(
                repo,
                library,
                version,
                previous.id,
                row.id,
                vector_store,
            )
        repo.update_job_status(job.id, "succeeded")
        repo.session.commit()
        return {
            "library": library,
            "version": version,
            "revision": row.revision,
            "source": "manual",
            "files": len(files),
            "ingest": report,
            "catalog_job_id": str(job.id),
            "upload_id": upload_id,
        }
    except Exception as exc:
        repo.session.rollback()
        repo.update_version_status(row.id, VersionStatus.FAILED)
        repo.update_job_status(job.id, "failed", error=str(exc)[:2000])
        repo.session.commit()
        log.exception("manual import failed for %s@%s", library, version)
        raise


def process_import(library: str, version: str, upload_id: str) -> dict:
    """Production RQ entrypoint; uses the existing sage-crawl worker."""
    from services.crawler.jobs import _open_repo, _real_ingest, _real_vector_store

    storage = ImportStorage()
    archive = storage.get(f"manual-imports/{upload_id}.zip", max_bytes=MAX_ARCHIVE_BYTES)
    repo = _open_repo()
    try:
        return process_import_impl(
            library,
            version,
            upload_id,
            archive,
            repo=repo,
            storage=storage,
            ingest=_real_ingest,
            vector_store=_real_vector_store(),
        )
    finally:
        repo.session.close()
