# mcp_server — FastMCP server (Streamable HTTP, NO auth). CPU only.
# The stdio adapter (src/mcp_server/stdio_adapter.py) runs as a separate
# local process, not in this image.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    PYTHONPATH=/app/src:/app
WORKDIR /app
COPY services/mcp_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/mcp_server/src/ ./src/
# services/catalog is copied in so LibraryResolver.from_catalog() works
# (Wave 5 fix: it imports services.catalog.CatalogRepository).
COPY services/__init__.py /app/services/__init__.py
COPY services/catalog/ /app/services/catalog/
RUN useradd -r -u 10001 sage && chown -R sage:sage /app
USER sage
EXPOSE 8000
# Required env: RETRIEVAL_URL (default http://retrieval:8001).
# Optional: SAGE_RESOLVER_TABLE (default: bundled libraries_seed.json),
# MCP_ALLOWED_ORIGINS (comma-separated, browser DNS-rebinding protection).
# Bind address/port come from MCP_HOST / MCP_PORT (compose port contract);
# default to 0.0.0.0:8000. Bind to localhost / tailnet interface via compose.
CMD ["sh", "-c", "uvicorn --factory mcp_server.server:create_app --host ${MCP_HOST:-0.0.0.0} --port ${MCP_PORT:-8000}"]
