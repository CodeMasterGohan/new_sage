"""RQ worker entrypoint for the crawler service.

Production: the worker consumes the ``sage-crawl`` queue (Redis) and runs
``jobs.crawl_library_version`` / ``jobs.refresh_stale`` /
``jobs.devdocs_sync`` per job.

Run:
    REDIS_URL=redis://redis:6379/0 DATABASE_URL=postgresql+psycopg://... \
      python -m services.crawler.worker

Enqueue from the MCP server / refresh scheduler:
    from services.crawler.jobs import enqueue_crawl
    enqueue_crawl(library_id, version)
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("sage.crawler.worker")


def _redis():
    try:
        import redis
    except ImportError as exc:
        raise RuntimeError("redis is not installed: the RQ worker needs it.") from exc
    url = os.environ.get("REDIS_URL", "")
    if not url:
        raise RuntimeError("REDIS_URL is not set: refusing to guess the queue.")
    return redis.from_url(url)


def main() -> None:
    from rq import Queue, Worker

    from .jobs import CRAWL_QUEUE

    conn = _redis()
    queues = [Queue(CRAWL_QUEUE, connection=conn)]
    worker = Worker(queues, connection=conn, name="sage-crawler")
    log.info("sage crawler worker started, listening on %s", CRAWL_QUEUE)
    worker.work()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
