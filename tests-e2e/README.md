# tests-e2e — New_sage end-to-end smoke tests

Proves the full spec §1 primary flow with fakes only (no network, no
Docker, no model weights, no Postgres):

```
seed (libraries_seed.json → SQLite CatalogRepository)
  → import_markdown_files → DocChunker → pipeline.ingest_pages
      (FakeEmbeddingClient, pipeline.FakeVectorStore, real
       CatalogWriterAdapter on SQLite)
  → RetrievalService.retrieve
      (SmokeVectorStore, FakeRetrievalEmbedding, SmokeCatalog,
       IdentityReranker)
  → FastMCP tools resolve-library-id + get-library-docs
      (build_fastmcp_for_tests + InMemoryRetrievalClient)
  → LibraryResolver exact / alias / typo-tolerance checks
```

## Run

From `~/workspace/new_sage/build`:

```bash
.venv-e2e/bin/python -m pytest tests-e2e -q
```

## Environment

`.venv-e2e/` (Python 3.12) carries exactly what the smoke needs and
nothing live: `pytest`, `pytest-asyncio`, `sqlalchemy`, `pydantic==2.11.7`,
`mcp==1.12.0`, `httpx`, `trafilatura`, `requests`. It is a test-only
virtualenv, not a service image — do not deploy it.

Note: `mcp==1.12.0` requires `pydantic==2.11.x` (the pinned combination
from `services/mcp_server/requirements.txt`); newer pydantic breaks
FastMCP's import.

## Files

- `conftest.py` — puts the build root and `services/mcp_server/src` on
  `sys.path` (mirrors the per-service conftest pattern).
- `fixtures/` — three hand-written Markdown docs (`fastapi.md`,
  `django.md`, `vue.md`), each templated with `{VERSION}` so the two
  ingested versions per library get distinct, version-marked content.
- `test_e2e_smoke.py` — the 18-test suite, in flow order:
  seed (1) → ingest (5) → retrieval (4) → MCP tools (5) → resolver (3).

## Known integration finding (fixed)

Repo-clone imports (§9.3.1) produce pages with `url=""`, but
`PassageProvenance.url` requires a non-empty URL — `get-library-docs`
raised a pydantic `ValidationError` on every repo-sourced passage.
Fixed in `services/mcp_server/src/mcp_server/tools.py`: fall back to the
library's `docs_url` for the provenance URL. Covered by
`test_mcp_empty_page_url_does_not_crash` (verified to fail without the
fix, pass with it).
