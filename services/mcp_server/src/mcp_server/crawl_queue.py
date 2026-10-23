"""Optional on-demand crawl dispatch for already registered libraries.

Disabled by default. SAGE_AUTO_CRAWL must be set to 1 by the operator.
No arbitrary user-supplied URLs are sent to the crawler.
"""
from __future__ import annotations

import os


def dispatch_crawl(library_id: str, version: str) -> str:
    """Enqueue a library/version; ten-minute Redis dedup prevents storms."""
    import redis
    from rq import Queue
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from services.catalog.repository import CatalogRepository

    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        raise RuntimeError("REDIS_URL is required when SAGE_AUTO_CRAWL=1")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required when SAGE_AUTO_CRAWL=1")
    # MCP uses canonical library slugs; the crawler accepts catalog UUIDs.
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            library = CatalogRepository(session).resolve_library(library_id)
            if library is None:
                raise ValueError(f"unknown registered library: {library_id}")
            catalog_id = str(library.id)
    finally:
        engine.dispose()
    connection = redis.from_url(redis_url)
    key = f"sage:pending-crawl:{catalog_id}:{version}"
    if not connection.set(key, "1", nx=True, ex=600):
        return "already-queued"
    try:
        job = Queue("sage-crawl", connection=connection).enqueue(
            "services.crawler.jobs.crawl_library_version",
            catalog_id, version, job_timeout=3600,
        )
        return job.id
    except Exception:
        connection.delete(key)
        raise
