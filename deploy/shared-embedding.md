# Shared GPU embeddings for SAGE and DRUID

Use SAGE's `docker-compose.shared-embedding.yml` overlay with the **single**
EmbeddingGemma 2 vLLM service maintained in DRUID. It runs on physical H200
GPU 0 with BF16, native pooling, text/image support, 768-dimensional output,
bounded batching, and disabled audio/video encoders.

The complete deployment, compatibility check, tuning and rollback guide is
[DRUID deploy/shared-embedding.md](https://github.com/CodeMasterGohan/new_DRUID/blob/main/deploy/shared-embedding.md).
Start that server once on the same Docker host; do not start one model per app.

1. Start the shared server and create `shared-embedding-net` as documented there.
2. Add its `EMBEDDING_VLLM_API_KEY` to SAGE's `deploy/.env`.
3. Verify CPU/vLLM vector parity and top-k retrieval on your own data before cutover.
4. From SAGE root run:

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml -f deploy/docker-compose.shared-embedding.yml up -d --build embedding
```

Include the overlay on subsequent deployments and in CI/Dockge definitions.
All existing query and document embedding API clients remain unchanged. The
adapter uses a lightweight image without Torch/Transformers; the original CPU
image remains available for rollback. Only the adapter joins the shared model
network. No GPU is required on the SAGE adapter itself. Model inference is on
GPU 0 in the shared service; reranking and parsing remain separate workloads.

This opt-in mode supersedes the original CPU-only embedding design. The base
Compose deployment remains the local CPU option. Neither index deletion nor
re-embedding happens automatically. To roll back, deploy with the original
Compose file alone and rebuild/recreate the embedding container.
