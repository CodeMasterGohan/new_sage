"""RQ worker entrypoint for the ingestion service.

Production: the worker consumes the ``sage-ingest`` queue (Redis) and runs
``pipeline.ingest_pages`` per job. Chunk IDs are content-derived, so a
retried job never duplicates Milvus records (idempotent upsert).

Run:
    REDIS_URL=redis://redis:6379/0 python -m services.ingestion.worker

Enqueue from the crawler/refresh jobs:
    from services.ingestion.worker import enqueue_ingest
    enqueue_ingest(library_id, version, pages, source)
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("sage.ingestion.worker")


def _redis():
    try:
        import redis
    except ImportError as exc:
        raise RuntimeError(
            "redis is not installed: the RQ worker needs it."
        ) from exc
    url = os.environ.get("REDIS_URL", "")
    if not url:
        raise RuntimeError("REDIS_URL is not set: refusing to guess the queue.")
    return redis.from_url(url)


def enqueue_ingest(
    library_id: str,
    version: str,
    pages: list,
    source: str,
    *,
    prev_version: str | None = None,
    job_timeout: int = 3600,
) -> str:
    """Enqueue one (library, version) ingestion job. Returns the job ID."""
    from rq import Queue

    from .models import Page  # noqa: F401  (keeps Page importable for RQ)

    conn = _redis()
    queue = Queue("sage-ingest", connection=conn)
    job = queue.enqueue(
        "services.ingestion.pipeline.ingest_pages",
        library_id,
        version,
        pages,
        source,
        prev_version=prev_version,
        job_timeout=job_timeout,
    )
    log.info("enqueued ingest job %s for %s@%s", job.id, library_id, version)
    return job.id


def main() -> None:
    from rq import Queue, Worker

    conn = _redis()
    queues = [Queue("sage-ingest", connection=conn)]
    worker = Worker(queues, connection=conn, name="sage-ingestion")
    log.info("sage ingestion worker started, listening on sage-ingest")
    worker.work()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
