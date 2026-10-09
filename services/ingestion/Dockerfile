# New_sage ingestion worker: RQ consumer running pipeline.ingest_pages.
# CPU-only image. Offline-first: the MiniLM tokenizer is pre-staged into the
# image at /artifacts/minilm-tokenizer and read via MINILM_TOKENIZER_PATH;
# the service fails closed if it is missing (never downloads).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    MINILM_TOKENIZER_PATH=/artifacts/minilm-tokenizer

WORKDIR /app

COPY services/ingestion/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Build context root is ~/workspace/new_sage/build.
COPY services/__init__.py /app/services/__init__.py
COPY services/ingestion /app/services/ingestion
COPY services/catalog /app/services/catalog

# Pre-staged artifacts (MiniLM tokenizer for the production token_len).
COPY artifacts/minilm-tokenizer /artifacts/minilm-tokenizer

ENV REDIS_URL="" \
    EMBEDDING_URL="" \
    MILVUS_URI="http://milvus:19530"

CMD ["python", "-m", "services.ingestion.worker"]
