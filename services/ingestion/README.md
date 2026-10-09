# New_sage ingestion service

Local-first docs ingestion for New_sage (spec §5–§7, §9.3.1). One
`(library, version)` at a time:

```
sources → Page → DocChunker → embedding service → Milvus sage_chunks
                                                     → catalog → activate
```

Sources: repo clones (`import_markdown_files`, spec §9.3.1), DevDocs docsets
(`import_devdocs_docset`, spec §5.1.1), tldr pages (`import_tldr_page`),
crawled HTML (`html_to_markdown`, spec §5.3), PDF manuals (`pdf_fallback`,
spec §5.4 — CPU-only, off by default).

## Layout

| File | Purpose |
|---|---|
| `html_to_md.py` | `html_to_markdown(html, url)`: trafilatura → readability fallback; code fences re-tagged with `language-*` from source HTML; nav/sidebars/footers dropped |
| `doc_chunker.py` | Heading-based chunker (H1/H2/H3 sections; 420-token target, 48-token prose overlap); **code blocks never split**; single code block >2000 tokens emitted whole with `oversized_code=True`; budget assertion on every normal chunk (violation = bug) |
| `identifiers.py` | Code-identifier extraction (function/class/method names) for the §9 identifier channel |
| `importers.py` | Seed importers → `Page` objects (repo markdown, DevDocs docset dir, tldr) |
| `pdf_fallback.py` | pdftext page-text extraction → one `Page` per PDF page → same chunker. pdftext import is lazy; clear error when missing |
| `pipeline.py` | `ingest_pages(library_id, version, pages, source, …)`: chunk → embed → Milvus upsert → catalog rows → activate version; `content_hash` dedup vs `prev_version` (§6) |
| `worker.py` | RQ worker (`sage-ingest` queue; `REDIS_URL`), `enqueue_ingest()` helper |
| `models.py` | `Page`, `Chunk` (+ `to_milvus_record()`), `index_text_for_chunk()` — the BM25 input-text composer (chunk text + headings + code identifiers) |

## Tokenizer / offline policy

The chunker is tokenizer-aware via an injected `token_len` callable.
Production wires the **pre-staged** MiniLM tokenizer (`MINILM_TOKENIZER_PATH`,
`local_files_only=True`); it fails closed when the artifact is missing —
nothing ever downloads. Tests inject a word counter.

## Milvus `sage_chunks` record shape

```python
{chunk_id, library_id, version_id, dense, text,
 headings,   # JSON string of the breadcrumb list
 url, token_count, ordinal}   # ordinal = chunk_order within the page
```

**Coordination note for the `milvus_setup` agent:** `sparse` is a
`SPARSE_FLOAT_VECTOR` generated server-side by the collection's BM25
function (`text_to_bm25`, input field `text`) — ingestion never writes it,
so the insert record omits `sparse` entirely. The `text` field carries the
BM25 input composition: chunk text + headings + extracted code identifiers
(`models.index_text_for_chunk`), since the function analyzes exactly what
is stored in `text`. `dense` is a 768-dim EmbeddingGemma 2 vector
(L2-normalized by the embedding service). `headings` is a JSON-encoded
string (Milvus `VARCHAR`), not an array field.

## Catalog seam

`pipeline.CatalogWriter` is the abstract interface the sibling catalog
package satisfies (`page_hashes`, `upsert_page`, `insert_chunks`,
`alias_page`, `activate_version`). `load_catalog_writer()` resolves
`services.catalog.make_catalog_writer()` — implemented in Wave 2
(`services/catalog/writer.py`, a `CatalogWriterAdapter` over
`CatalogRepository`, tested on SQLite). `ingest_pages` auto-resolves it
when no `catalog=` is passed (in-process — writers hold a SQLAlchemy
engine and are not RQ-serializable); when the sibling package or
`DATABASE_URL` is unavailable it falls back to `NullCatalogWriter`
(records in memory, warns in the report — never silently pretends).

## Embedding contract

`HttpEmbeddingClient` posts to `{EMBEDDING_URL}/embed_documents` with
`{"items": [{"text", "title", "is_code"}], "task": "question_answering"}`.
(The request key is `items`, matching the embedding service's
`EmbedDocumentsRequest` — `documents` is rejected with 422.)
The embedding service applies the EmbeddingGemma 2 PrefixContract
server-side (`web_search_document` for chunks). Dimension is verified
(768); a mismatch fails closed.

## Run

```bash
# tests (from ~/workspace/new_sage/build)
./services/ingestion/.venv/bin/python -m pytest services/ingestion/tests -q

# worker
REDIS_URL=redis://redis:6379/0 EMBEDDING_URL=http://embedding:8000 \
  python -m services.ingestion.worker
```

## Deviations from the task/spec

1. **Oversized code: emitted, not skipped; operative threshold is the budget.**
   Spec §7 says "skip blocks > 2000 tokens with a warning"; the task says
   emit whole with an `oversized_code` flag, never silently truncate. The
   task's explicit instruction won (emit whole + flag). The flag fires for
   any code block that cannot fit the hard budget — not only >2000 tokens —
   because a block in (budget, 2000] is equally unsplittable ("never split
   mid-block") and must not trip the budget assertion. The >2000 figure is
   the spec's extreme case; every flagged block is reported in the ingest
   report's warnings.
2. **Budget assertion scope.** new_DRUID's hard budget (reranker max −
   query reserve) is kept as `hard_budget_tokens` (default 512) for normal
   chunks; packing targets 420 per §17.4. The assertion raises
   `AssertionError` on violation (violation = bug), matching DRUID's
   discipline. Overlap is dropped (not forced) when carrying the tail would
   break the budget — verified by a 300-doc fuzz run.
3. **Chunk text does not embed the breadcrumb prefix.** DRUID prepends the
   heading path to embedding text; here headings travel in their own Milvus
   field (`headings` JSON) and are sent as the embedding request's `title`,
   per §9's three-channel (text + headings + identifiers) indexing.
4. **pdftext not exercised in tests** — pdftext isn't installed in the test
   venv (it's a Docker-image dependency); the missing-dependency guard is
   tested instead. The import follows new_DRUID's lazy-import pattern and
   the same `dictionary_output` API.
5. **Sibling catalog doesn't exist yet** (its directory is empty), so the
   pipeline depends on the `CatalogWriter` protocol with an in-memory
   `NullCatalogWriter` default and a documented `make_catalog_writer()`
   contract for the catalog agent.
