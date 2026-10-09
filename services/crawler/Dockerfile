# New_sage crawler worker: RQ consumer running jobs.crawl_library_version.
# CPU-only image. Offline-first: no models, no downloads at runtime —
# git is the only extra system dependency (repo-clone pinning, §9.3.1).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/crawler/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Build context root is ~/workspace/new_sage/build.
COPY services/__init__.py /app/services/__init__.py
COPY services/crawler /app/services/crawler
COPY services/catalog /app/services/catalog
COPY services/ingestion /app/services/ingestion

ENV REDIS_URL="" \
    DATABASE_URL="" \
    MINIO_ENDPOINT="" \
    MINIO_ACCESS_KEY="" \
    MINIO_SECRET_KEY="" \
    MINIO_BUCKET="sage-snapshots"

CMD ["python", "-m", "services.crawler.worker"]
