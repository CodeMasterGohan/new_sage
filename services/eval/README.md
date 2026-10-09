# new_sage eval service (P4) — spec §16

Model-free eval harness + starter corpus for the §16 eval gate. Everything
here runs on a plain VM with **no model weights, no network, no live
infra**; the dense/rerank-dependent measurements are explicitly stubbed as
BLOCKED until the H200 run.

## Layout

```
services/eval/
  corpus.py                 corpus loader + schema validation
  metrics.py                recall_at_k, version_correctness, resolver_accuracy
  harness.py                EvalHarness + CLI (--branch bm25 | hybrid)
  requirements.txt          pytest only (sibling services supply the rest)
  sample_report_bm25.json   baseline BM25-branch report (2026-10-08, this VM)
  corpus/
    eval_corpus.jsonl       50 hand-written questions (10 libs × 2 versions)
    resolver_cases.jsonl    41 resolver cases (exact/alias/typo/ecosystem/unknown)
    docs/                   20 hand-written Markdown docs, one per (lib, version)
  tests/
    test_metrics.py         pure metric unit tests
    test_corpus.py          corpus schema + coverage + verbatim-gold tests
    test_harness.py         fake-rigged harness tests (incl. BLOCKED hybrid)
```

## Corpus format (`corpus/eval_corpus.jsonl`)

One JSON object per line:

```json
{"id": "react-01", "library": "react", "version": "19.2.0", "type": "how",
 "question": "How do I run side effects ...?",
 "gold_passage_text": "useEffect is a React Hook ... (1-4 sentences)",
 "gold_url": "https://react.dev/reference/react/useEffect",
 "gold_section": ["Reference", "useEffect"]}
```

- `type`: `how` (topic retrieval), `changed` (version discrimination),
  `signature` (identifier/BM25 precision) — the three spec §16 types.
- `gold_passage_text` must appear **verbatim** in
  `corpus/docs/<library>__<version>.md` (a test enforces this); the harness
  finds the chunk containing it and uses that chunk's id as the gold id for
  recall@k.
- Current size: **50 questions = 10 libraries × 2 versions × 5**
  (20 how / 10 changed / 20 signature), libraries from the MCP seed table:
  react, fastapi, django, numpy, pandas, pytorch, typescript, go,
  postgres, redis.

### Extending to the full 150

1. Append JSONL rows (unique `id`s; `validate_item` rejects bad rows).
2. Add/extend `corpus/docs/<library>__<version>.md` so every new
   `(library, version)` pair has a doc and every gold passage is verbatim in
   it. Keep a few distractor sections per doc so retrieval must discriminate.
3. Run `pytest tests/` — the corpus tests check schema, coverage, sentence
   counts, and verbatim containment.

Caveat: gold passages are representative excerpts hand-written from knowledge
of these libraries' docs for offline harness calibration. **Spot-check them
against the real docs before the H200 gate** — especially the `changed`
items, which encode version deltas from memory.

## Metrics (`metrics.py`)

- `recall_at_k(ranked_ids, gold_id, k)` → 1.0/0.0.
- `version_correctness(retrieved_version, asked_version)` → exact-match bool;
  a fallback to `is_latest` counts as incorrect.
- `resolver_accuracy(cases, resolver)` → `{total, correct, accuracy, failures}`.
- `mean(values)` helper.

## Harness (`harness.py`)

Wires the **real** components with injected fakes:

- real `LibraryResolver` (mcp_server) over `libraries_seed.json`;
- real `RetrievalService` (retrieval) with `FakeCatalog` (seed-backed version
  resolution), `FakeEmbedding` (768-dim zero vectors — dense contributes
  nothing), `FakeVectorStore` (tiny real BM25 ranker over chunk
  text + 2× headings; **records every `VersionPredicate` it receives** so
  version routing is audited, §9.2), `FakeReranker` (no-op — RRF order kept);
- real `DocChunker` (ingestion) with deterministic `word_len` token_len.

```bash
# BM25 branch — runs NOW (no models needed)
python harness.py --branch bm25 [--ks 1 3 5 10] [--report-out report.json]

# hybrid branch — dense + MiniLM rerank; exits 2 BLOCKED without weights
python harness.py --branch hybrid

# tests (use the mcp venv: has pytest + pydantic)
~/workspace/new_sage/build/.venv-mcp/bin/python -m pytest tests/ -q
```

The BM25 branch reports: recall@k overall and by question type,
version-routing correctness (predicate audit), resolver accuracy,
chunk calibration (≤420-token budget, code-block intactness), and the
explicit BLOCKED list for the H200 run.

## Baseline (this VM, 2026-10-08)

```
branch=bm25  items=50
  recall@1: 0.940   recall@3: 1.000   recall@5: 1.000   recall@10: 1.000
  how:       @1=0.950   changed: @1=1.000   signature: @1=0.900
  version_routing: 50/50 = 1.000
  resolver_accuracy: 35/41 = 0.854  (6 typo cases below fuzzy auto-accept)
  chunks: n=90 max_tokens=86  budget<=420: True  code_intact=True
```

The 3 recall@1 misses all rank the gold chunk 2nd (within-doc section
confusion, e.g. the useEffect prose section outranking its Parameters
section) — recall@3 is perfect. **This is a BM25-only FLOOR, not the
headline**: dense vectors and MiniLM reranking are absent here and should
only improve on it.

## BLOCKED for the H200 run

- **Dense retrieval** — no EmbeddingGemma 2 weights on this VM (downloading
  forbidden). `FakeVectorStore.dense_search` returns no hits.
- **MiniLM rerank** — no MiniLM weights (`MINILM_MODEL_PATH` unset).
  `FakeReranker` returns equal scores.
- `--branch hybrid` exits code 2 with a BLOCKED message until
  `MINILM_MODEL_PATH` and `MODEL_PATH`/`EMBEDDING_MODEL_PATH` point at
  non-empty staged artifact dirs; the live hybrid wiring (real embedding
  client + MiniLM reranker against Milvus) still needs to be written for
  that run.
- Production tokenizer: the harness uses word-count `token_len`; the H200
  run should wire the pre-staged MiniLM tokenizer like production does.
