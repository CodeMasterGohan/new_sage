# New_sage refresh scheduler: runs services.crawler.jobs.refresh_stale()
# in-process once per REFRESH_INTERVAL_SECONDS (default 7 days).
#
# CPU-only, offline-first image. It needs the crawler's dependency closure
# because refresh_stale() imports services.crawler.jobs in-process
# (SQLAlchemy/psycopg for the catalog, redis/rq to enqueue crawls, the
# S3 snapshot client via jobs' module imports) — pins match the crawler
# image exactly. No git binary: the scheduler only enqueues jobs; the
# crawler-worker clones.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PYTHONPATH=/app/services:/app

WORKDIR /app

COPY services/crawler/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Build context root is ~/workspace/new_sage/build.
COPY services/__init__.py /app/services/__init__.py
COPY services/scheduler /app/services/scheduler
COPY services/crawler /app/services/crawler
COPY services/catalog /app/services/catalog

ENV REDIS_URL="" \
    DATABASE_URL="" \
    REFRESH_INTERVAL_SECONDS="604800" \
    SCHEDULER_STARTUP_JITTER_SECONDS="600"

CMD ["python", "-m", "scheduler.run"]
