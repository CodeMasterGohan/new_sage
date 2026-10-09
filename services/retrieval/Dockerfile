# retrieval — hybrid version-scoped search + RRF + MiniLM rerank (FastAPI). CPU only.
# The services tree layout is preserved because the code uses relative
# imports (from .catalog import ...); the app runs as services.retrieval.main.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
WORKDIR /app
COPY services/retrieval/requirements.txt .
# CPU-only torch: install torch from the CPU index FIRST (matches the
# embedding service's pin set), then the rest of the requirements.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.6.0 \
 && pip install --no-cache-dir -r requirements.txt
COPY services/__init__.py /app/services/__init__.py
COPY services/retrieval/ /app/services/retrieval/
EXPOSE 8001
CMD ["uvicorn", "services.retrieval.main:app", "--host", "0.0.0.0", "--port", "8001"]
