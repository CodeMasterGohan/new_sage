"""Authenticated upload API; never shares the unauthenticated MCP port."""

from __future__ import annotations

import os
import secrets
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job

from .core import (
    MAX_ARCHIVE_BYTES,
    MAX_FILES,
    ImportValidationError,
    make_archive,
    read_documents,
    safe_path,
    validate_identity,
)
from .storage import ImportStorage

app = FastAPI(title="SAGE Documentation Admin", docs_url=None, redoc_url=None, openapi_url=None)


def require_admin(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("SAGE_ADMIN_TOKEN", "")
    if not expected or len(expected) < 32:
        raise HTTPException(
            status_code=503, detail="SAGE_ADMIN_TOKEN must be configured (32+ chars)"
        )
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail="Admin bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )


def queue_connection():
    import redis

    url = os.environ.get("REDIS_URL")
    if not url:
        raise RuntimeError("REDIS_URL is required")
    return redis.from_url(url)


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(
        Path(__file__).with_name("ui.html"),
        media_type="text/html",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/admin/imports", status_code=202, dependencies=[Depends(require_admin)])
async def import_documents(
    library: Annotated[str, Form()],
    version: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
):
    try:
        library, version = validate_identity(library, version)
        if len(files) > MAX_FILES:
            raise ImportValidationError("too many files")
        members: list[tuple[str, bytes]] = []
        received_bytes = 0
        for file in files:
            name = safe_path(file.filename or "")
            content = await file.read(MAX_ARCHIVE_BYTES + 1)
            if len(content) > MAX_ARCHIVE_BYTES:
                raise ImportValidationError("uploaded file exceeds 64 MiB limit")
            received_bytes += len(content)
            if received_bytes > MAX_ARCHIVE_BYTES:
                raise ImportValidationError("total upload exceeds 64 MiB limit")
            if name.lower().endswith(".zip"):
                if len(files) != 1:
                    raise ImportValidationError(
                        "upload either one ZIP or multiple Markdown/HTML files"
                    )
                archive = content
                break
            members.append((name, content))
        else:
            archive = make_archive(members)
        # Reject unsafe/empty ZIPs before storing/enqueueing any work.
        read_documents(archive)
    except ImportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        for file in files:
            await file.close()

    upload_id = str(uuid.uuid4())
    storage = ImportStorage()
    storage.put(f"manual-imports/{upload_id}.zip", archive, "application/zip")
    queue = Queue("sage-crawl", connection=queue_connection())
    job = queue.enqueue(
        "services.manual_import.jobs.process_import",
        library,
        version,
        upload_id,
        job_timeout=7200,
        result_ttl=86400,
        failure_ttl=604800,
    )
    return {
        "job_id": job.id,
        "library": library,
        "version": version,
        "status": "queued",
        "status_url": f"/admin/imports/{job.id}",
    }


@app.get("/admin/imports/{job_id}", dependencies=[Depends(require_admin)])
def get_import_status(job_id: str):
    try:
        job = Job.fetch(job_id, connection=queue_connection())
    except (NoSuchJobError, ValueError):
        raise HTTPException(status_code=404, detail="Import job not found") from None
    if job.func_name != "services.manual_import.jobs.process_import":
        raise HTTPException(status_code=404, detail="Import job not found")
    status = job.get_status(refresh=True)
    return {
        "job_id": job.id,
        "status": getattr(status, "value", status),
        "result": job.return_value() if job.is_finished else None,
        "error": "Import failed; inspect crawler-worker logs" if job.is_failed else None,
    }
