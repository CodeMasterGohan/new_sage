# CPU integration testing

SAGE already runs extraction, embeddings and reranking on CPU. No GPU mode flag
is required to test those paths. Real pretrained inference still requires the
approved model artifacts to be staged locally; production never downloads them.

The live CPU pipeline test connects HTML extraction, structural chunking,
PostgreSQL catalog writes, Milvus indexing, activation and version-scoped BM25
retrieval:

```bash
SAGE_TEST_DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:15432/druid_test \
MILVUS_URI=http://127.0.0.1:19530 \
python -m pytest -q tests-e2e/test_live_cpu_pipeline.py
```

Use only disposable test infrastructure. This test creates and drops its own
PostgreSQL schema and Milvus collection, and requires the normal lightweight
test dependencies plus `pymilvus==2.5.6`. It skips when connection variables or
the SDK are absent. The example PostgreSQL connection is for the local onboarding
stack, which uses test-only authentication.

Vectors and token counts are deterministic fixtures so the test exercises real
storage/search integration without model weights. It does not measure semantic
retrieval or reranker quality. Model inference tests require verified artifacts
and remain distinct from this test. The onboarding environment's network policy
currently blocks the Hugging Face model source.
