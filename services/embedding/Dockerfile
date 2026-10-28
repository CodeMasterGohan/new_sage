# EmbeddingGemma 2 API adapter; shared vLLM performs inference by default.
# Optional local CPU inference is installed only when explicitly requested.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    OMP_NUM_THREADS=8
WORKDIR /app
ARG INSTALL_LOCAL_EMBEDDINGS=false
COPY services/embedding/requirements*.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && if [ "$INSTALL_LOCAL_EMBEDDINGS" = "true" ]; then \
      pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.6.0 torchvision==0.21.0 \
      && pip install --no-cache-dir -r /tmp/requirements-local.txt; \
    fi
# App layout: real "services.embedding" package so relative imports work
# (uvicorn services.embedding.main:app). The contract loader (reuse.py)
# registers its own "sage_contract" stub packages at runtime — no clash.
COPY services/__init__.py /app/services/__init__.py
COPY services/embedding/__init__.py /app/services/embedding/__init__.py
COPY services/embedding/reuse.py /app/services/embedding/reuse.py
COPY services/embedding/service.py /app/services/embedding/service.py
COPY services/embedding/settings.py /app/services/embedding/settings.py
COPY services/embedding/vllm_backend.py /app/services/embedding/vllm_backend.py
COPY services/embedding/main.py /app/services/embedding/main.py
# §8 prefix contract: vendored byte-identical copies of the verified
# new_DRUID sources. Loaded by reuse.py without executing any package init.
# Never retype or edit these files — byte-identity is the contract guarantee.
COPY services/embedding/vendor/ingestion/embeddings.py /app/services/embedding/vendor/ingestion/embeddings.py
COPY services/embedding/vendor/ingestion/chunker.py /app/services/embedding/vendor/ingestion/chunker.py
COPY services/embedding/vendor/ingestion/config.py /app/services/embedding/vendor/ingestion/config.py
COPY services/embedding/vendor/ingestion/tree.py /app/services/embedding/vendor/ingestion/tree.py
COPY services/embedding/vendor/ingestion/identifiers.py /app/services/embedding/vendor/ingestion/identifiers.py
# Only the optional local backend needs a read-only model artifact mount.
EXPOSE 8002
CMD ["uvicorn", "services.embedding.main:app", "--host", "0.0.0.0", "--port", "8002"]
