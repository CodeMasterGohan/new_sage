# Embedding service — EmbeddingGemma 2 (spec §8)

Lightweight FastAPI adapter. By default it sends inference to the same
Docker vLLM `google/embeddinggemma-2` instance used by DRUID at
`http://vllm-embedding:8000/v1`. The adapter keeps the existing SAGE API and
prefix contract, and does not load another model or request GPU resources.
See [deployment instructions](../../deploy/README.md) for the shared server.

## The §8 contract (read this before touching prefixes)

The task-aware asymmetric prefix pairs are **contractual**. The wrong prefix
is a quality bug, not a cosmetic choice. The contract lives in exactly one
place — `vendor/ingestion/embeddings.py` (`PrefixContract`,
`build_query_text`, `build_document_text`) — byte-identical copies of the
verified new_DRUID sources, whose strings were verified verbatim against
the released `google/embeddinggemma-2` checkpoint. This service reuses them
via `reuse.py`. It does not duplicate the prefix strings; a test pins the
vendored file's sha256 against accidental edits.

| Kind | Task | Formatted input |
|---|---|---|
| document | `question_answering` (default) | `title: {heading_path} \| text: {chunk}` |
| document | `code_retrieval` | `title: {heading_path} \| text: {code}` (code path) |
| query | `question_answering` (default) | `task: question answering \| query: {q}` |
| query | `code_retrieval` | `task: code retrieval \| query: {q}` |
| query | `search_result` (approved alt) | `task: search result \| query: {q}` |

## Endpoints (exact HTTP contract)

Internal base URL: `http://embedding:8002` (not host-published by Compose).

### `POST /embed`

Embed **query-side** texts. The §8 query prefix is applied server-side,
selected by `task` — clients never send prefixes.

Request:
```json
{"texts": ["how do I do X with library L?"], "task": "question_answering"}
```
- `texts`: non-empty list of strings (each ≤ `EMBEDDING_MAX_INPUT_CHARS` chars; inputs must arrive pre-chunked, §7).
- `task`: `question_answering` (default) | `code_retrieval` | `search_result`. Unknown values → 422.

Response (200):
```json
{"vectors": [[0.012, ...]], "dimension": 768}
```
- `vectors[i]` is the 768-dim L2-normalized vector for `texts[i]`.
- Empty `texts` → 422; over-long input → 400.

### `POST /embed_documents`

Embed **document-side** items (chunks). The §8 document prefix is applied
server-side: `title: {title} | text: {text}` (code template when `is_code`
is true or `task` is `code_retrieval`).

Request:
```json
{
  "items": [
    {"title": "API Reference > Client > connect()", "text": "Connects the client.", "is_code": false},
    {"title": "example.py", "text": "def f():\n    pass", "is_code": true}
  ],
  "task": "question_answering"
}
```
- `items`: non-empty list of `{"title" (default ""), "text" (required), "is_code" (default false)}`.
- `task`: same three values; it selects the query-side prefix family and forces the code document path when `code_retrieval`.

Response (200): same shape as `/embed` — `{"vectors": [...], "dimension": 768}`.

### `GET /health`

Response (200):
```json
{"model": "google/embeddinggemma-2", "dimension": 768, "backend": "vllm", "device": "remote", "model_loaded": true}
```
- `model_loaded` is true only after the first vector passes the width + normalization guard (the startup warmup does this).
- 503 while the backend is not ready.

## Configuration (env)

| Var | Default | Notes |
|---|---|---|
| `MODEL_PATH` | (required for `st`) | Pre-staged artifact dir, read-only mount. `EMBEDDING_MODEL_PATH`, `EMBEDDING_MODEL_DIR` accepted as aliases. |
| `EMBEDDING_MODALITY_CONFIG` | `text+image` | or `text-only` (fallback) |
| `EMBEDDING_DIMENSION` | `768` | Must be a validated Matryoshka width (768/512/256/128) |
| `EMBEDDING_BACKEND` | `vllm` | Shared inference. `st` = optional local sentence-transformers; `mock` = deterministic fake for tests. |
| `EMBEDDING_VLLM_URL` | `http://vllm-embedding:8000/v1` | OpenAI-compatible API base URL, including `/v1`. |
| `EMBEDDING_VLLM_API_KEY` | empty | Optional bearer credential. |
| `EMBEDDING_VLLM_TIMEOUT_S` | `120` | Request timeout; errors fail closed without a local fallback. |
| `EMBEDDING_BATCH_SIZE` | `32` | |
| `EMBEDDING_MAX_CONCURRENCY` | `4` | semaphore around the encode lock |
| `EMBEDDING_MAX_INPUT_CHARS` | `32768` | hard reject above this — inputs arrive pre-chunked (§7) |
| `EMBEDDING_PORT` | `8002` | |

Startup **fails fast** when vLLM is unavailable or returns invalid vectors.
It checks the model ID, count, ordering, width and finite numeric components
on every response. No adapter downloads model artifacts. The opt-in local
`st` backend still requires a pre-staged read-only artifact.

## Matryoshka

Coded for 768 dims. A validated width change (512/256/128) plugs into
`EMBEDDING_DIMENSION` with re-normalization. The vLLM client always requests
native 768-dimensional vectors, validates them, then truncates and
normalizes locally as the existing sentence-transformers backend does.
It omits the upstream `dimensions` parameter because the released model
config does not advertise `is_matryoshka` and vLLM rejects it. Any width
change requires full re-embedding per the migration rule — the service will
not silently mix widths.

## Docker

The image copies the vendored contract files
(`services/embedding/vendor/ingestion/{embeddings,config,chunker,tree,identifiers}.py`)
byte-identically from the verified new_DRUID sources. `reuse.py` loads them
as a `sage_contract.*` stub package — no clash with the real `services`
package. Build context is the repository root.

Run against the already-started shared server:
```bash
docker build -f services/embedding/Dockerfile -t new-sage-embedding .
docker run --rm --network embedding-net \
  -e EMBEDDING_VLLM_URL=http://vllm-embedding:8000/v1 \
  new-sage-embedding
```

For the optional local CPU backend, build with
`--build-arg INSTALL_LOCAL_EMBEDDINGS=true`, set `EMBEDDING_BACKEND=st`,
and mount the staged model read-only at `MODEL_PATH`. The default image
installs only the HTTP adapter dependencies; it contains no PyTorch,
Transformers or sentence-transformers runtime.
