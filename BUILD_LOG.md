# New_sage BUILD_LOG

## 2026-10-08 — Build authorized; Wave 1 (P1 skeleton) dispatched (coordinator)

James authorized the full build ("try to complete it"). Spec: `~/workspace/new_sage/new_sage_architecture_v1.md` v1, all §17 decisions resolved 2026-10-08 (Context7-exact tool names, H200 deploy, full DevDocs+MDN+tldr seed, 420/48 chunking, rerank 50, automated weekly refresh + monthly DevDocs sync, pdftext PDF fallback CPU-only).

Wave 1: 7 parallel service agents, all adapting `~/workspace/new_druid/build/` code with tenancy/auth stripped:
- catalog — Postgres + Alembic: library, library_version, page, chunk, crawl_job (no tenant_id/group_id/ACLs)
- mcp_server — FastMCP, `resolve-library-id` + `get-library-docs`, no auth; hand-written `libraries_seed.json` (~15 libs)
- retrieval — dense+BM25+identifier, RRF, MiniLM rerank 50; tenant predicate → (library_id, version_id) predicate; POST /retrieve contract
- embedding — port of druid CPU EmbeddingGemma 2 service; POST /embed, POST /embed_documents, GET /health
- ingestion — trafilatura HTML→MD, heading chunker (§7, 420/48, code never split), 3 importers on tiny fixtures, pipeline w/ fake-friendly interfaces, pdftext fallback stub
- deploy + milvus_setup — compose stack (mcp 8000, retrieval 8001, embedding 8002, pg, redis, minio, milvus standalone; no GPU), `sage_chunks` schema, deletion helpers
- artifacts — embeddinggemma-2 + minilm manifests (from druid completed/), new seed-corpus.yaml with digest-resolution procedures

Shared contracts dictated: Milvus `sage_chunks` fields, embedding/retrieval HTTP contracts, port map, env vars. Constraints: CPU-only, no weight downloads, fail-closed artifacts, no GitHub pushes, py3.11+, tests per service.

Pending: Wave 1 results → Wave 2 (P2 seed importers on small samples + e2e retrieval) → Wave 3 (P3 crawler) → Wave 4 (P4 eval) → Wave 5 (P5 harden).

## 2026-10-08 — Wave 1 partial: deploy + milvus_setup DONE (7/7)

Deploy agent completed: docker-compose.yml (10 services: mcp-server 8000, retrieval 8001, embedding 8002, ingestion-worker, postgres, redis, minio, milvus-standalone 19530/9091 + internal etcd, sage-net, named volumes, health-gated depends_on, no GPU), .env.example with *_HOST_PORT remaps for H200 coexistence with new_DRUID, deploy README. milvus_setup: sage_chunks schema exactly per contract (chunk_id PK, library_id/version_id, dense 768 HNSW COSINE, sparse SPARSE_FLOAT_VECTOR w/ BM25 function, analyzer text), create_collection.py (idempotent), deletion.py (library/version scoped, ID validation). Tests: 20 passed, 1 skipped (live Milvus). Compose validated via YAML-parse assertions (no docker on VM).
Deviations: (1) internal etcd service added — milvus 2.6.x standalone requires external etcd; (2) host ports overridable via env, container ports fixed; (3) dense metric COSINE (matches new_DRUID v2.1); (4) no GitHub push, no downloads.
Remaining Wave 1: catalog, mcp_server, retrieval, embedding, ingestion, artifacts (6 agents running).

## 2026-10-08 — Wave 1 partial: embedding DONE (4/7)

Embedding agent completed: faithful port of druid CPU EmbeddingGemma 2 service to services/embedding/ with exact contract (POST /embed query-side, POST /embed_documents doc-side, GET /health). PrefixContract reused byte-identically via vendor/ingestion/ (sha256-pinned test). Pins: sentence-transformers==6.1.0, transformers==5.18.0, torch==2.6.0. Port 8002, HF offline, fail-closed. Tests: 23 passed, 0 failed (mock backend).
Deviations: (1) dropped /embed_image (not in sage contract); (2) split /embed into query/doc endpoints per contract; (3) fixed latent druid Dockerfile bug — copies services.embedding package, runs uvicorn services.embedding.main:app (druid's top-level main:app broke on relative imports); (4) contract vendored under services/embedding/vendor/ingestion/ until ingestion service lands — reuse.py re-pointable.
Note: fail-closed message names EMBEDDING_MODEL_PATH while primary env is MODEL_PATH (both resolve; cosmetic).
Remaining Wave 1: catalog, mcp_server, retrieval, ingestion, artifacts (5 running).

## 2026-10-08 — Wave 1 partial: catalog DONE (3/7)

Catalog agent completed: SQLAlchemy 2.x models (Library, LibraryVersion, Page, Chunk, CrawlJob + VersionSource/VersionStatus/JobStatus/JobKind enums), full CatalogRepository (resolve_library w/ alias+ecosystem, version idempotency, mark_version_ready/latest-flip, page/chunk recording, crawl jobs, list_stale_versions, delete_version cascade prune), Alembic 0001_initial_schema (5 tables, constraints, indexes, downgrade), Dockerfile/requirements (sqlalchemy 2.0.38, alembic 1.15.2, psycopg 3.2.9). Tests: 43 passed, 0 failed (SQLite-backed, migration cross-checked vs metadata).
Deviations: (1) ORM-level cascade prune instead of passive_deletes (SQLite FK enforcement); (2) client-side ARRAY defaults + PG DEFAULT '{}'; (3) no services/common dependency — repository takes bare Session; (4) TEXT columns + enum validation at repo layer, no native PG enums.
Remaining Wave 1: mcp_server, retrieval, ingestion, artifacts (4 running).

## 2026-10-08 — Wave 1 partial: retrieval DONE (4/7)

Retrieval agent completed: service.py (version resolution → single VersionPredicate on all 3 branches → RRF k=60 + identifier boost → MiniLM rerank 50 → token-budget truncation, ≥1 passage kept), predicates.py, catalog.py (2 tiny SQL queries, no invented HTTP API), identifiers.py (druid patterns + library_name_mentions), fusion/reranker/embedding_client/milvus_store, main.py POST /retrieve + GET /health on 8001. PrefixContract byte-identical (README-documented). Tests: 30 passed, 0 failed (fakes only).
Deviations/notes: (1) code_language derived at render time from first fenced block (no schema column — OK); (2) OPEN: overview "first chunks by ordinal" needs ordinal INT64 in Milvus sage_chunks schema — catalog HAS ordinal, Milvus does NOT → Wave 5 fix (milvus_setup schema + retrieval store); (3) bm25_search relies on deploy's BM25 function on sparse (fail-closed otherwise); (4) aliases read from library.aliases TEXT[] — matches catalog. No GitHub push.
Remaining Wave 1: mcp_server, ingestion, artifacts (3 running).

## 2026-10-08 — Wave 1 partial: mcp_server DONE (5/7)

MCP agent completed: FastMCP src/ layout, no auth anywhere (Origin check only). models.py (arg validation, PassageProvenance with url+section, trust marker, injection rejection), resolver.py (§9.3: exact → ecosystem hint → BM25+trigram fuzzy w/ typo tolerance → unknown+crawl hint), clients.py (HTTP client vs dictated /retrieve contract + in-memory fake), tools.py (exact Context7 tool names; get-library-docs never fails on unknown/stale — version_fallback + crawl_recommended flags; token-budget enforced), server.py (8000, no auth), stdio adapter, libraries_seed.json (15 libs). Tests: 63 passed, 0 failed.
Deviations: (1) resolver is file-backed on libraries_seed.json — catalog landed in parallel AFTER this agent ran; DB-backed loader is a clean seam for Wave 5; (2) added trigram fuzzy tier for typos (no new deps); (3) provenance delimiters inline in passage text + structured fields. No GitHub push.
Remaining Wave 1: ingestion, artifacts (2 running).

## 2026-10-08 — Wave 1 partial: ingestion DONE (6/7)

Ingestion agent completed: html_to_md.py (trafilatura + readability fallback; re-attaches language-x tags trafilatura strips), doc_chunker.py (H1/H2/H3 sections, ATX+setext, fenced code atomic, 420/48, budget assertion, sha1 chunk IDs, injected token_len, fail-closed tokenizer), identifiers.py (code-identifier extractor), bm25.py (local BM25 sparse {token_id:weight}, headings x2, identifiers x3), importers.py (markdown-files, devdocs docset, tldr — all on tiny hand-built fixtures), pdf_fallback.py (lazy pdftext, CPU-only), pipeline.py (chunk → BM25 → embed_documents → Milvus upsert → catalog → activate; content_hash cross-version dedup; VectorStore/CatalogWriter ABCs), worker.py (RQ sage-ingest queue). Tests: 27 passed, 0 failed; 300-doc fuzz clean (caught + fixed 2 real bugs: overlap tail budget blowout, over-budget code path gap).
Deviations: (1) oversized code emitted whole + oversized_code flag (task won over spec's skip->2000); flag fires for any code over hard budget; (2) overlap dropped (not forced) when tail would break budget; (3) CatalogWriter protocol seam (catalog landed in parallel); (4) pdftext import path untested (not in test venv). No GitHub push, no crawling.
Remaining Wave 1: artifacts (1 running).

## 2026-10-08 — WAVE 1 COMPLETE (P1 skeleton)

All 7 service agents done. Test totals: catalog 43, mcp_server 63, retrieval 30, embedding 23, ingestion 27, milvus_setup 20 (+1 skipped live), artifacts 51 → **257 passed, 1 skipped, 0 failed.**

Artifacts agent: manifests/embeddinggemma-2.yaml + minilm-l6-v2.yaml (byte-identical from druid completed/), NEW seed-corpus.yaml with all 3 pins RESOLVED 2026-10-08 (DevDocs ghcr digest sha256:8623d9b9…b3d66a60, MDN fd0b11ad…, tldr 6bbd7cc4… — metadata-only lookups, no downloads). Validator extended: seed_sources schema, defer_to_staging mechanism, DOCUMENTED_STAGING_DEFERRALS registry, `unresolved`/`verify-seed` CLI cmds. Tests: 51 passed. Note: service-side fail-closed startup assertions documented but land with services later; APPROVAL-2026-101 pending James sign-off (same as druid).

Wave 2 (P2 integration) + P3 (crawler) dispatched in parallel:
- wiring agent: cross-service contract verification (embedding↔retrieval↔ingestion, catalog imports, milvus schema↔stores, compose env↔settings, MCP↔/retrieve), ordinal INT64 fix across milvus_setup + retrieval + ingestion, rerun all suites.
- crawler agent: services/crawler/ — polite fetch (robots.txt, 1rps, backoff), version detection (Sphinx/Docusaurus//latest//git tags), repo-clone pinning (§9.3.1), RQ crawl jobs, stale-while-revalidate, MinIO snapshots, monthly DevDocs sync diff logic.

## 2026-10-08 — P3 crawler DONE (8/9)

Crawler agent completed services/crawler/: polite_fetch.py (robots.txt fail-closed on disallow, 1rps/host, backoff w/ Retry-After, NewSageBot UA), version_detect.py (Sphinx versions.json, Docusaurus, /latest/ redirect, git ls-remote tags), repo_clone.py (clone --branch --depth 1, tag injection-validated, README+docs/**/*.md+CHANGELOG → ingestion importers), snapshots.py (MinIO ABC + in-memory fake), stale.py (mirrors list_stale_versions), jobs.py (crawl_library_version w/ repo-clone preference + v-prefix tag rules, refresh_stale, devdocs_sync w/ pure devdocs_diff; docker pull stubbed production-only), worker.py (sage-crawl queue). Tests: 55 passed, 0 failed (offline; jobs tests drive real CatalogRepository on SQLite).
Deviations: (1) re-crawls update version row in place — UNIQUE(library_id, version) blocks "new row per re-crawl" for same version string (spec §4 says immutable; flagged as spec tension); (2) robots.txt fetch-failure fails open, disallow fails closed; (3) enqueue_crawl in jobs.py; (4) repo-clone snapshots = commit SHA in job report, no MinIO tarball; (5) catalog status walk doesn't mirror pipeline-internal stages.
Gaps flagged by agent: DevDocs image-manifest parsing is a stub (NotImplementedError); commit SHAs have no catalog column (needs Alembic migration); no per-library concurrency cap beyond per-host throttle.
Remaining: wiring agent (1 running).

## 2026-10-08 — Wave 2 wiring DONE (9/9)

Wiring agent: full contract verification + fixes. Test totals now **276 passed, 2 skipped, 0 failed** (retrieval 41, embedding 23, catalog 48, ingestion 29, milvus_setup 21+1skip, mcp_server 63+1skip, artifacts 51). With crawler's 55: **331 passed, 2 skipped.**

FIXED: (1) ingestion posted {"documents"} to /embed_documents — service requires {"items"} → would 422 every ingest; fixed + regression test. (2) retrieval catalog SQL referenced nonexistent library.library_id/version_id columns → rewritten vs real DDL; version_id is now the VERSION STRING (matches ingestion's sage_chunks writes + druid document_version pattern). (3) ingestion make_catalog_writer() seam now implemented (services/catalog/writer.py CatalogWriterAdapter; auto-resolves in-process; Dockerfile copies services/__init__.py; requirements += sqlalchemy/psycopg). (4) Milvus ordinal INT64 applied (schema + ingestion record + retrieval overview ordering). (5) compose: DATABASE_URL standardized to postgresql+psycopg:// (SQLAlchemy) w/ retrieval stripping suffix for raw psycopg3; added missing catalog-migrate one-shot; MCP CMD honors MCP_HOST/PORT; retrieval Dockerfile fixed (was flattening to /app w/ broken relative imports — same latent bug embedding agent found in druid). (6) MCP resolver DB seam done: LibraryResolver.from_catalog(database_url), verified on SQLite.
PASS: MCP↔retrieval contract, PrefixContract byte-identity (sha256 1c830ef8…).

OPEN — BM25 design divergence (needs James's call, code untouched): ingestion computes sparse locally (plain SPARSE_FLOAT_VECTOR, no analyzer); milvus_setup attached server-side BM25 function text_to_bm25; retrieval REQUIRES the function (fail-closed). Can't coexist (function-generated field can't be client-inserted). Recommendation: adopt function design (drop sparse from ingestion inserts; fold headings/identifiers into text for §9.1 weighting).
Other opens: deletion.py version_id validation relaxed for dots; Milvus-side page aliasing (§6) has no VectorStore protocol method yet; mcp-server Docker image still needs services/catalog copied + sqlalchemy/psycopg in requirements (docstring-noted).

## 2026-10-08 — E2E smoke DONE (P2/P4 evidence)

End-to-end smoke agent: `tests-e2e/` (new top-level dir, not per-service).
**18 passed, 0 failed**, fakes + SQLite only — no network, docker, or model
weights. Run: `.venv-e2e/bin/python -m pytest tests-e2e -q` (dedicated
`.venv-e2e`, pins match service requirements: pydantic==2.11.7, mcp==1.12.0).

Path proven: seed 3 libs × 2 versions (SQLite CatalogRepository from
libraries_seed.json) → import_markdown_files → DocChunker (word-count
token_len) → ingest_pages (FakeEmbeddingClient w/ real /embed_documents
{"items"} shapes, pipeline FakeVectorStore, real CatalogWriterAdapter) →
RetrievalService.retrieve (SmokeVectorStore applying one VersionPredicate
per branch, fake embedder, identity reranker) → FastMCP
resolve-library-id + get-library-docs via call_tool → resolver
exact/alias/typo checks.

FIXED (integration bug, small): repo-clone imports (§9.3.1) produce pages
with url="" but PassageProvenance.url requires min_length=1 → every
repo-sourced passage crashed get-library-docs with a pydantic
ValidationError. Fix in services/mcp_server/src/mcp_server/tools.py:
fall back to the library's docs_url for the provenance URL. Regression
test test_mcp_empty_page_url_does_not_crash verified to fail without the
fix and pass with it. All 64 existing mcp_server contract tests still pass.

Assertions covered: heading breadcrumbs, code fences intact (```python/```js),
sage_chunks record key shape {chunk_id, library_id, version_id, dense,
sparse, text, headings, url, token_count, ordinal} with 768-dim dense,
catalog page/chunk rows + ready/is_latest flags (older version ingested
first since activate_version flips is_latest every ingest), version scoping
(v1 query returns v1-only disjoint content), version_fallback on None/unknown,
overview mode (score None, getting-started first), token-budget ≥1 passage,
exact Context7 tool names, provenance delimiters (library/version/url/section),
unknown library → graceful (found:false on resolve; empty passages +
crawl_recommended on docs), unknown version → version_fallback:true +
crawl_recommended:true.

## 2026-10-08 — E2E smoke DONE (10/11)

E2E agent built tests-e2e/ (18 tests, all pass, ~1.5s, fakes+sqlite only): seed 3 libs × 2 versions from libraries_seed.json → import_markdown_files → DocChunker → ingest_pages (fake embedder w/ real /embed_documents shapes, fake vector store, REAL CatalogWriterAdapter on SQLite) → RetrievalService.retrieve (predicate-scoped fake store) → both FastMCP tools via call_tool. Asserts: breadcrumbs, intact code fences, exact sage_chunks key shape, ready + exactly-one-latest, version scoping/fallback, overview mode, token budget, exact Context7 tool names, inline provenance, graceful unknown handling, exact/alias/typo resolution.
BUG FOUND + FIXED: repo-clone pages have url="" (§9.3.1 path) but PassageProvenance.url required min_length=1 → ValidationError on every repo-sourced passage. Fixed in mcp_server/tools.py: url=hit.url or record.docs_url (regression test fails without, passes with). Note: ingest order matters — activate_version flips is_latest per ingest, so ingest older versions first.
Remaining: P4 eval (1 running).

## 2026-10-08 — P4 eval DONE (11/11)

Eval agent built services/eval/: corpus.py/metrics.py/harness.py + corpus/ (50 questions = 10 libs × 2 versions × 5; types: how=20, changed=10, signature=20; gold passages 1-4 sentences, verbatim in docs), 41 resolver cases, 20 markdown docs. Tests: 28 passed, 0 failed.
NOW-runnable numbers (no models): BM25 recall@1=0.940, recall@3/5/10=1.000 (by type: how .950, changed 1.000, signature .900; 3 @1 misses rank gold 2nd — within-doc section confusion, recovered @3); version routing 50/50=1.000 (incl. is_latest fallback path); resolver accuracy 35/41=0.854 (6 misses = typos below fuzzy auto-accept threshold, returns alternatives by design — threshold may want H200 calibration); chunk-budget 100% compliant (90 chunks, max 86 tokens, code intact).
BLOCKED for H200: dense retrieval (no EG2 weights), MiniLM rerank (no weights), --branch hybrid exits 2, live hybrid wiring unwritten, harness uses word-count token_len (production needs MiniLM tokenizer).
Caveats: gold passages hand-written — spot-check vs real docs before H200 gate (esp. changed-type deltas); corpus docs small by design; extend to 150 per README.

## 2026-10-08 — P5 hardening DONE (final pass before build report)

Fixes applied (all in ~/workspace/new_sage/build/):
1. mcp-server Docker gap (Wave 2 open item): Dockerfile now copies
   services/__init__.py + services/catalog/ into the image, PYTHONPATH is
   /app/src:/app, and requirements.txt gains sqlalchemy==2.0.38 +
   psycopg[binary]==3.2.9 (catalog's exact pins) — LibraryResolver.from_catalog()
   now runs in Docker. Stale "not done in Wave 2" docstring note updated.
2. commit_sha column (crawler gap): LibraryVersion model gains
   commit_sha TEXT NULL; new Alembic migration
   services/catalog/alembic/versions/0002_commit_sha.py (upgrade adds the
   column, downgrade drops it); new services/catalog/tests/test_migration_0002.py
   (4 tests: upgrade applies cleanly on SQLite via a minimal alembic.op shim
   emitting the migration's own DDL, SHA round-trips incl. NULL default,
   downgrade drops the column, model/DB column parity). All pass.
   Follow-up seam (not in P5 scope): repository.get_or_create_version() takes
   no commit_sha kwarg — the crawler can set the plain mapped attribute
   directly, or a setter can be added later.
3. Dockerfile/entrypoint audit (all 8 images): entrypoints verified —
   mcp 8000 / retrieval 8001 / embedding 8002 EXPOSE match the port contract;
   no service flattens to /app with a broken top-level module (retrieval's
   earlier flatten bug stays fixed; artifacts' `python -m src.cli` and
   milvus_setup's `python -m milvus_setup.create_collection` are proper
   packages). Two fixes: crawler Dockerfile was missing services/__init__.py
   (relied on PEP 420 namespace packages) and its requirements lacked the
   ingestion runtime deps its lazy services.ingestion imports need at crawl
   time — added trafilatura==2.3.1, readability-lxml==0.9, lxml==6.1.3,
   lxml_html_clean==0.4.5, pymilvus==2.4.6, pdftext==0.4.1, pypdfium2==4.30.0
   (ingestion's exact pins).
4. Compose: ADDED the missing crawler-worker service (12 services now) — the
   crawler Dockerfile and the sage-crawl RQ queue existed but nothing in the
   stack ran services.crawler.worker, so enqueue_crawl/refresh_stale jobs
   would pile up in Redis unprocessed. Wired with REDIS_URL, DATABASE_URL,
   MINIO_* (mapped to the minio service's ROOT credentials), health-gated
   depends_on (postgres, catalog-migrate completed, redis, minio).

Final test totals (full rerun, 0 failed): catalog 52 (48 + 4 new),
mcp_server 63+1 skipped, retrieval 41, embedding 23, ingestion 29,
milvus_setup 21+1 skipped (live), artifacts 51, crawler 55, tests-e2e 18,
eval 28 → **381 passed, 2 skipped, 0 failed.**
Commands (from build root): catalog + crawler via services/crawler/.venv
(sqlalchemy 2.0.36); mcp_server via .venv-mcp with
PYTHONPATH=services/mcp_server/src; ingestion via services/ingestion/.venv;
retrieval/embedding/milvus_setup via /tmp/sage-test-venv (fastapi/httpx/
pydantic/pytest); artifacts via /tmp/sage-artifacts-venv (pyyaml/pytest);
tests-e2e + eval via .venv-e2e.

Compose validation: no docker on VM → YAML-parse + assertion checks
(12 services present, container ports 8000/8001/8002, all build dockerfiles
exist, env wiring incl. new crawler-worker, health-gated depends_on, named
volumes declared, depends_on targets resolve) — ALL PASSED. Note: the
declared `sage-net` network is unattached (services use the implicit default
network; inter-service DNS unaffected).

Pin consistency (manual pass — pip-compile needs network: BLOCKED, not
installed and no network): every requirements.txt now fully == pinned
(eval's prose `pytest>=8` fixed to pytest==9.1.1). Consistent across services:
sqlalchemy 2.0.38, psycopg[binary] 3.2.9, sentence-transformers 6.1.0,
transformers 5.18.0, fastapi 0.115.8, pydantic 2.11.7, httpx 0.28.1, rq 2.12.0,
redis 8.1.0, requests 2.34.2, trafilatura stack. Divergences (separate images,
no hard conflicts — flagged, not changed): pymilvus 2.4.6 (ingestion/crawler)
vs 2.5.6 (retrieval/milvus_setup); uvicorn 0.34.0 (embedding/retrieval) vs
0.34.3 (mcp_server); pytest 8.3.4/8.3.5/9.1.1 (test-only).

NOT verified (gap list): docker image builds (no docker on VM — Dockerfiles
audited statically only); alembic upgrade against real Postgres (migration
chain exercised on SQLite via shim; 0001 is Postgres-only by design);
pymilvus 2.4.6 vs 2.5.6 unification (recommend at H200 deploy);
pip-compile closure (blocked: no network).
Untouched per instructions: BM25 function-vs-local-sparse divergence (needs
James's call); re-crawl in-place vs §4 immutability spec tension (noted only).
No GitHub pushes made.

## 2026-10-09 — BM25 goes server-side only (James's decision #1)

Resolved the BM25 divergence: ingestion-side local sparse vectors are dead;
the Milvus collection's server-side `text_to_bm25` function is the only BM25
path. Untouched per instructions: `services/milvus_setup/schema.py` (function
spec: input field `text` -> output `sparse`) and `services/retrieval`
(the bm25 branch requires the function, fails closed without it).

Changes:
- Deleted `services/ingestion/bm25.py` (local `build_sparse_vectors`,
  `sparse_vector`, `tokenize`, `token_id`, `build_index`).
- `services/ingestion/pipeline.py`: removed the `build_sparse_vectors`
  import and the local-BM25 stage from `ingest_pages` (replaced with a NOTE
  pointing at the server-side function).
- `services/ingestion/models.py`: removed the `Chunk.sparse` dataclass field;
  `to_milvus_record()` no longer emits a `sparse` key. The `text` field now
  carries the BM25 input composition via `index_text_for_chunk()` (moved here
  from the deleted module, composition unchanged: chunk text + headings x2 +
  code identifiers x3). `identifiers.py` extraction is untouched — the chunker
  still populates `chunk.identifiers`, which now flows into the server-side
  BM25 analyzer through `text`. `Chunk.text` itself stays raw (embedding
  input unchanged); only the Milvus `text` field is augmented.
- `services/ingestion/README.md`: layout table and record-shape/coordination
  notes rewritten for the server-side contract.
- Tests: `services/ingestion/tests/test_bm25.py` rewritten to pin the NEW
  contract (composition contains all three channels with the weighting
  repetition; record has no `sparse` key; record shape matches the
  function-wired schema). `test_pipeline.py` expectations updated (no
  `sparse`; `text` carries headings + the `run` identifier).
  `tests-e2e/test_e2e_smoke.py`: `EXPECTED_RECORD_KEYS` minus `sparse`;
  shape test asserts no `sparse` in records.

Test results (all green):
- ingestion: 30 passed (was 29; +1 net from rewritten test_bm25)
- retrieval: 41 passed (untouched, requirement side preserved)
- milvus_setup: 21 passed, 1 skipped (live test, unchanged)
- tests-e2e: 18 passed
- crawler: 55 passed (consumes ingest_pages; external contract unchanged)

Behavioral caveats:
- Retrieved passages now render the augmented `text` (headings/identifiers
  appended, repeated for BM25 weight) — slightly noisier display, but it also
  improves the identifier branch's LIKE matching on `text`.
- Token-budget accounting in get-library-docs uses the augmented text length
  (it is what Milvus stores and returns).
- The old local path's per-batch IDF statistics are gone; BM25 weighting is
  now whatever the Milvus server-side analyzer computes over the collection.

## 2026-10-09 — APPROVAL-2026-101 signed off (James, chat)

James approved the three seed-corpus pins in seed-corpus.yaml: DevDocs ghcr
image digest sha256:8623d9b9…, MDN content @ fd0b11ad (CC-BY-SA), tldr-pages
@ 6bbd7cc4 (CC-BY-SA 4.0). Manifest approval_record updated to reflect sign-off.

## 2026-10-09 — Spec §4 revision immutability implemented (James-approved decision)

Re-crawls previously UPDATED the `library_version` row in place (deviation:
spec §4 says version rows are "immutable once indexed", and
UNIQUE(library_id, version) blocked new-row-per-re-crawl). Now re-crawls
INSERT a new revision row; existing rows are never mutated.

Schema: `library_version.revision` INTEGER NOT NULL DEFAULT 1 (Alembic
0003, existing rows backfilled to 1); unique constraint moved from
UNIQUE(library_id, version) to UNIQUE(library_id, version, revision).

Active-version rule: exactly one revision is active per (library, version)
— the newest READY row wins (`get_active_version`); an explicitly
requested version string always resolves to its active revision, else
falls back to is_latest (§9.2). `get_version_by_string` now returns the
newest revision any-status (ingestion writers resolve the in-flight row).
`list_stale_versions` only considers active revisions (superseded rows are
never re-crawled). Retrieval SQL filters `status='ready'` ORDER BY
revision DESC.

FK decision (step 4): each revision gets FRESH page+chunk rows — no
cross-revision aliasing within the same version string. Rationale:
aliasing would entangle revisions (pruning rev N would cascade-delete
chunk rows rev N+1 references); fresh rows keep every revision
self-contained, byte-identical, and independently prunable. §6
cross-version dedup (different version strings) is unchanged. Milvus
orphan handling: chunk IDs are content-derived so unchanged chunks upsert
idempotently; after a successful re-crawl the crawler deletes chunk IDs
present in the old revision but absent from the new one
(`VectorStore.delete_chunks`, batched `chunk_id in [...]` filter) with no
serving gap (runs post-READY, stale-while-revalidate preserved).

Drive-by fix (required by the above): the crawler passed
`str(library.id)` (UUID) as the ingest `library_id`, but retrieval
predicates filter Milvus by the canonical slug and the ingestion
CatalogWriter contract documents slug — crawler-ingested chunks were
unretrievable. The crawler now passes `lib.name` everywhere
(ingest/pages/importer); `RepoCatalogWriter.page_hashes` resolves the slug
and reads the active revision.

Tests: catalog 66 passed (+8 revision tests incl. byte-identical-old-row
proof, +5 migration tests), crawler 57 passed (+2 re-crawl/prune tests),
retrieval 41 passed, ingestion 30 passed, milvus_setup 21 passed.
Pre-existing env gaps (unchanged): mcp_server/eval/artifacts suites need
httpx/pydantic/yaml, not installed on this VM.

## 2026-10-09 — Logging audit + gap-fill across all 9 services (James's request)

Uniform stdlib `logging` instrumentation applied in the crawler's house
style (`log = logging.getLogger(__name__)`, %-format args, INFO/WARNING/
ERROR/DEBUG, `log.exception` in handlers). No `print()` for diagnostics
remains in service code (eval score report and artifacts verdict lines
keep their stdout/stderr output contract). No silent `except:` existed;
all remaining silent `except Exception: return/continue` paths now log at
DEBUG. No new dependencies.

Per service:
- mcp_server (was zero logging): `server.py` startup banner (retrieval_url,
  resolver_table, allowed_origins count, dev-fake flag, no-auth note) +
  WARNING on disallowed-Origin rejections; `tools.py` request lifecycle for
  both tools (start/finish IDs, elapsed on get-library-docs), WARNING on
  unknown library_id and retrieval outage, INFO on version_fallback=True;
  `resolver.py` INFO on seed-file/catalog loads, ambiguous resolutions,
  fuzzy auto-accepts with the typo candidate list, and unknowns; `clients.py`
  ERROR with base_url + library@version context on transport/malformed
  responses; `stdio_adapter.py` startup banner + crash logging, ALL on
  stderr (stdout stays MCP protocol-clean).
- catalog: `repository.py` INFO on library creation, version row creation,
  new immutable revision INSERT (with revision number), mark_version_ready
  (+ latest flip), job enqueue, and job terminal transitions (FAILED at
  ERROR with the error text); `writer.py` INFO on version activation
  (revision number) + DEBUG on page/chunk writes. `models.py` left alone
  (declarative schema, no runtime events).
- eval: `harness.py` `print()` diagnostics converted to logging; start/
  finish lifecycle with elapsed; final score report stays on stdout.
  `corpus.py`/`metrics.py` left alone (pure helpers).
- artifacts: `cli.py` lifecycle logging per command (start with manifest
  path, OK/failure with counts) on stderr via basicConfig; OK:/INVALID:/
  FAILED:/WARNING: verdict lines keep their stdout/stderr contract.
- retrieval: `main.py` startup banner with real config (port, milvus_uri,
  collection, embedding_url, embed_dim, rerank_candidates, rrf_k, CPU,
  no-auth) — DATABASE_URL deliberately NOT logged; `/retrieve` start/finish
  lifecycle with IDs, version_fallback, passage count, elapsed; WARNING on
  unknown library. `service.py` retrieve() start/finish + WARNING on
  version_fallback triggers (DependencyError paths already logged by the
  /retrieve handler — no duplication).
- embedding: `main.py` ERROR context on /embed + /embed_documents 500s
  (startup banner + mock-backend warning already present).
- ingestion: `pipeline.py` ingest start log + elapsed on both completion
  paths (main + aliased-only); `html_to_md.py` silent parse-fallback
  excepts now log DEBUG with URL context. `worker.py` left alone (already
  has startup + enqueue logs).
- crawler: `jobs.py` sitemap-fetch silent `continue`s → DEBUG; the two
  silent excepts in `version_detect.py` → DEBUG. Otherwise left alone (the
  house standard: enqueue, crawl start/finish, refresh sweep, DevDocs sync,
  prune, backoff/retry context already logged).
- milvus_setup: `deletion.py` gained the service's second logger — INFO on
  delete_library/delete_version (expr + server-reported delete_count).
  `create_collection.py` and `schema.py` left alone (provisioning already
  fully logged; schema builders are pure).

Drive-by env fix: the /tmp test venvs from the last full run were gone
(ephemeral), so pip cached wheels were used to install pyyaml + fastapi
into the existing `.venv-mcp` — no new downloads needed. This unblocks the
previously-blocked mcp_server/eval/artifacts suites on this VM.

Tests (full rerun, 0 failed): catalog 66, crawler 57, ingestion 30,
mcp_server 63+1 skipped, retrieval 41, embedding 23, milvus_setup 21+1
skipped, artifacts 51, eval 28, tests-e2e 18 → **399 passed, 2 skipped,
0 failed** (identical to pre-edit baseline). Smoke-verified: eval harness
score report on stdout / diagnostics on stderr; artifacts CLI verdict on
stdout / lifecycle on stderr; MCP server banner + typo-suggestion + tool
lifecycle logs; stdio adapter stderr-only enforced via basicConfig stream.
