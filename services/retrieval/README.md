# new_sage retrieval service

Hybrid version-scoped documentation retrieval (spec §9): dense (EmbeddingGemma 2)
+ BM25 + identifier branches → RRF fusion → MiniLM rerank (top 50, CPU) →
token-budget truncation. Port **8001**.

## HTTP API

`POST /retrieve`

```json
{
  "library_id": "react",
  "version": "19.0.0",     // optional; unresolvable/None -> is_latest, version_fallback=true
  "topic": "useEffect cleanup",  // optional; omit -> overview mode
  "top_k": 50,
  "token_budget": 10000
}
```

Response:

```json
{
  "library": {"library_id": "react", "name": "react", "ecosystem": "npm"},
  "version": "19.0.0",
  "version_fallback": false,
  "passages": [
    {"text": "...", "url": "https://...", "section": ["Hooks", "useEffect"],
     "code_language": "jsx", "score": 4.21}
  ]
}
```

- **Version resolution** (§9.2): exact version string → its `version_id`;
  unresolvable/`null` → the `is_latest` version with `version_fallback: true`.
  Versions come from Postgres (`DATABASE_URL`) — two queries, no catalog HTTP API.
- **No topic → overview mode**: top chunks from pages whose `headings`
  breadcrumb indicates getting-started/overview (fallback: predicate-scoped
  query, store order). No embedding call, no rerank; `score` is `null`.
- **Pipeline** (topic queries): embed query → dense + BM25 + identifier
  searches with ONE identical `(library_id, version_id)` predicate →
  RRF fusion (k=60) → identifier boost (+1.0) → MiniLM rerank top-50 →
  `top_k` finalists → token-budget truncation (always keeps ≥1 passage).
- `code_language` is derived from the passage's first fenced code block
  (ingestion preserves language tags verbatim, §5.3).
- No authentication (§13). Errors: 404 unknown library, 502 dependency
  failure, 500 internal.

## Embedding contract (spec §8)

The client sends **raw text + task string**; the embedding service applies
the contractual **PrefixContract** prompt strings **server-side**.
The strings below are byte-identical to new_DRUID's
`services/ingestion/embeddings.py` `PrefixContract` — documented here so the
contract is auditable in one place, not duplicated in code:

| Side     | Task          | Prompt string                              |
|----------|---------------|--------------------------------------------|
| document | prose chunk   | `title: {heading} \| text: {text}`         |
| document | code chunk    | `title: {heading} \| text: {text}`         |
| query    | `question_answering` | `task: question answering \| query: {query}` |
| query    | `code_retrieval`     | `task: code retrieval \| query: {query}`     |
| query    | `search_result`      | `task: search result \| query: {query}`      |

The heading for document prefixes is the ` > `-joined heading breadcrumb
(falling back to the page title). Query-side task is chosen by
`prompts.detect_task` (explicit `task_hint` wins, else a conservative code
heuristic); the document-side prefixing is the ingestion agent's job.

Embedding HTTP contract (owned by the embedding service):

- `POST /embed` ← `{"texts": [...], "task": "question_answering"}` →
  `{"vectors": [...], "dimension": 768}`
- `POST /embed_documents` ← `{"items": [{"title","text","is_code"}], "task"}` →
  `{"vectors": [...]}` (ingestion agent; retrieval never calls it)

## Module layout (flat)

| File | Role |
|------|------|
| `main.py` | FastAPI `POST /retrieve`, `GET /health`; production wiring (port 8001) |
| `service.py` | `RetrievalService.retrieve()` — the §9 sequence |
| `models.py` | `StoredChunk`, `ScoredHit`, `FusedCandidate`, `Passage`, `RetrieveResponse`, `detect_code_language` |
| `predicates.py` | `VersionPredicate(library_id, version_id)` → Milvus filter expr |
| `interfaces.py` | `EmbeddingClient`, `VectorStore`, `Reranker`, `LibraryCatalog` ABCs |
| `catalog.py` | `PostgresLibraryCatalog` (the two SQL queries) |
| `fusion.py` | RRF + identifier boost (reused verbatim from new_DRUID) |
| `identifiers.py` | identifier-token extraction + library name/alias mention matching |
| `prompts.py` | query task detection (prefixes are server-side) |
| `reranker.py` | `rerank_candidates` + `MiniLMReranker` (CPU, pre-staged, fail-closed) |
| `embedding_client.py` | `HttpEmbeddingClient` (`EMBEDDING_URL`, default `http://embedding:8002`) |
| `milvus_store.py` | `MilvusVectorStore` over the `sage_chunks` schema |
| `config.py` | `RetrievalConfig.from_env()` |
| `errors.py` | `UnknownLibraryError`, `VersionResolutionError`, `DependencyError` |

## Milvus `sage_chunks` schema (dictated to the deploy agent)

```
chunk_id    VARCHAR(64) PK
library_id  VARCHAR(64)
version_id  VARCHAR(64)
dense       FLOAT_VECTOR(768)
sparse      SPARSE_FLOAT_VECTOR   ← BM25 function attached by deploy (full-text search)
text        VARCHAR(65535)
headings    VARCHAR(4096)         ← JSON breadcrumb, e.g. ["API Reference","Client","connect()"]
url         VARCHAR(2048)
token_count INT64
ordinal     INT64                  ← chunk order within page (Wave 2)
```

The `ordinal` field (added Wave 2) makes the overview-mode fallback
deterministic: when no getting-started/overview headings match, the store
returns the predicate-scoped rows ordered by `ordinal` ascending (sorted
client-side — pymilvus `query()` has no ORDER BY).

## Environment

| Var | Default | Purpose |
|-----|---------|---------|
| `DATABASE_URL` | — (required) | Postgres catalog |
| `EMBEDDING_URL` | `http://embedding:8002` | embedding service |
| `MILVUS_URI` | `http://milvus:19530` | Milvus |
| `MILVUS_COLLECTION` | `sage_chunks` | collection name |
| `MILVUS_TOKEN` | — | Milvus auth token (optional) |
| `MINILM_MODEL_PATH` | — (required) | pre-staged MiniLM cross-encoder dir |
| `MINILM_TOKENIZER_PATH` | = model path | pre-staged tokenizer dir |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` | `1` | enforced; no downloads ever |
| `RETRIEVAL_*` | see `config.py` | tuning knobs (candidates, RRF k, boost, dims) |

## Tests

```bash
python -m pytest tests/ -q
```

All fakes — no Milvus, models, or Postgres. Covers: identical version
predicate on all branches, version fallback flag, RRF fusion, token-budget
truncation, overview mode, identifier boost on library-name mentions,
unknown-library/version failures, embedding-width fail-closed, and the
response contract.
