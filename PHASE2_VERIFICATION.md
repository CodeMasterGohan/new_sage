# Phase 2: code-aware retrieval, provenance and evaluation

This work extends the same branch/PR as Phase 1. The retrieval index schema
does **not** change; no Milvus collection migration is required.

## 1. Better code and API reference retrieval

- `services/ingestion/identifiers.py` recognizes backtick-quoted API names
  and Terraform/Kubernetes-style `snake_case` symbols, including names in
  H1–H3 reference headings even without code fences.
- `services/retrieval/code_search.py` extracts high-signal symbols from the
  query (dotted members, function calls, snake_case, mixed-case identifiers).
- All dense, BM25, and identifier searches retain the **same** library and
  version predicate. The cross-encoder remains the primary ranking model;
  exact symbols earn a configurable additive bonus when the retrieved
  heading or definition contains the symbol.
- Tune `RETRIEVAL_CODE_SYMBOL_BONUS` (default 0.35; from-env hard range 0–1).
  Run a before/after evaluation on real code questions before changing it.
  Setting it to `0` preserves pure cross-encoder rankings.

**Existing indexed docs** contain indexed headings and some identifiers, but
new ingestion is needed for backtick and snake_case enrichment to take
effect in the BM25 channel. Query-aware reranking requires no reindex.

## 2. Verifiable repo-clone citations

New repo-clone ingestions attach immutable source file URLs for supported
public HTTPS hosts:

- GitHub `https://github.com/<owner>/<repo>/blob/<commit>/<file>`
- GitLab `https://gitlab.com/<owner>/<repo>/-/blob/<commit>/<file>`

URLs are built from checked-out files **relative** to the repository root
and the verified Git commit hash. Unknown hosts retain the existing empty
URL behavior (rather than inventing a potentially false citation).
The MCP's existing homepage fallback still applies when no document URL is
available. This does **not** yet provide exact source line ranges; adding
those requires ingestion offsets and index/provenance schema updates.

Old indexed repo-clone pages require a recrawl to gain the pinned URLs.
Verify deduplicated/aliased pages preserve the desired source revision.

## 3. Diagnostic retrieval traces

`POST /retrieve` accepts an optional `diagnostics` boolean. The
request only returns a diagnostic object if the retrieval service itself
has `SAGE_RETRIEVAL_DIAGNOSTICS=1` in its environment.

Example request:
```json
{"library_id":"react","version":"19.2.0","topic":"useEffect() cleanup","diagnostics":true}
```

The trace contains version-scoped candidate counts, ranks of top hits,
fusion scores, reranker scores (including any symbol boost), extracted
symbols and stage latency. Normal requests retain their existing response
contract. Diagnostics reveal internal ranking metadata; keep the retrieval
service private on the Compose network.

## 4. Real-service retrieval benchmark

`services/eval/live_retrieval.py` sends requests to a **running** retrieval
service using `services/eval/corpus/live_smoke_queries.jsonl`. Unlike the
earlier BM25-only fake harness, it exercises the live embeddings, Milvus,
and reranker when the service is deployed.

```bash
python services/eval/live_retrieval.py \
  --api http://127.0.0.1:8001 \
  --data services/eval/corpus/live_smoke_queries.jsonl \
  --k 5 --report-out /tmp/sage-phase2-eval.json
```

Replace the API URL with a trusted endpoint reachable from the host running
the evaluator. The retrieval service is deliberately not exposed publicly.

Results: correct served version with no fallback, syntactically valid citation
URL rate, `expected_symbol` hit@k, optional exact `expected_source_url`
hit@k, request failures, and p50/p95 end-to-end latency. The 10 example
queries are **smoke checks**, not a verified ground-truth benchmark.
Before treating them as quality gates, curate substantially more questions
against the actual ingested official docs, covering multiple versions and
technologies (including Terraform, Ansible, Kubernetes and SDKs once indexed).

**These metrics do not measure generated answer factuality, hallucination
rate or citation faithfulness.** Those require a separate agent evaluation
set with verified expected answers and citation spans.

## 5. Validation and rollout

Run CI plus the broader tests:

```bash
PYTHONPATH=.:services/mcp_server/src pytest -q \
  services/retrieval/tests/test_retrieval.py \
  services/retrieval/tests/test_code_search.py \
  services/ingestion/tests/test_symbol_index.py \
  services/crawler/tests/test_repo_provenance.py \
  services/eval/tests/test_live_retrieval.py
```

On H200, compare before/after baselines using a pinned snapshot of the same
corpus, index, and model weights. Review symbol false positives, p95
latency, exact version matching and source URLs. Do not merge or deploy
until those live acceptance checks succeed.
