# New_sage ingestion worker: RQ consumer running pipeline.ingest_pages.
# CPU-only. Offline-first tokenizer is bind-mounted read-only by Compose;
# clean repository checkouts must never require untracked model artifacts.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    MINILM_TOKENIZER_PATH=/artifacts/minilm

WORKDIR /app

COPY services/ingestion/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Build context root is ~/workspace/new_sage/build.
COPY services/__init__.py /app/services/__init__.py
COPY services/ingestion /app/services/ingestion
COPY services/catalog /app/services/catalog

# The pre-staged MiniLM directory is mounted at /artifacts by Compose.
# No model weights or tokenizer files are baked into the image.

ENV REDIS_URL="" \
    EMBEDDING_URL="" \
    MILVUS_URI="http://milvus:19530"

CMD ["python", "-m", "services.ingestion.worker"]
