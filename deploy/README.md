# New_sage deploy

CPU application stack for the local-first Context7 alternative.
**No auth on MCP.** EmbedGemma inference uses the GPU vLLM service shared
with DRUID; start it first using [shared-embedding.md](shared-embedding.md).
Application services speak plain HTTP on `sage-net`; the host firewall /
Tailscale interface is the security boundary. Do not expose these ports to the public internet.

## Service map

| Service | Container port | Host port | What it is |
|---|---|---|---|
| `mcp-server` | 8000 | `${MCP_HOST_PORT:-8010}` | FastMCP: `resolve-library-id`, `get-library-docs` (spec §10); the only published port (AI CLI) |
| `retrieval` | 8001 | — (internal-only) | RRF hybrid (dense+BM25+identifier) + MiniLM rerank (spec §9) |
| `embedding` | 8002 | — (internal-only) | EmbeddingGemma 2 adapter → shared vLLM, 768-dim |
| `ingestion-worker` | — | — | RQ worker: crawl → trafilatura → chunk → embed → index (spec §5) |
| `milvus-standalone` | 19530 / 9091 | — (internal-only) | `sage_chunks` collection (spec §9.1) |
| `rustfs` | 9000 / 9001 | — (internal-only) | raw snapshots + seed checkouts (spec §11) |
| `postgres` | 5432 | — (internal-only) | catalog: libraries, versions, pages, jobs (spec §11) |
| `redis` | 6379 | — (internal-only) | crawl queue + hot-response cache (spec §11) |
| `etcd` | — | — | internal only; required by milvus-standalone |

Private app network `sage-net`, plus `embedding-net` on the embedding
adapter for shared inference; persistent data in host bind mounts under
`/DATA/docker/mounts/sage/<service>` (postgres, redis, rustfs, etcd, milvus,
model-artifacts) — no named volumes.

Milvus (`milvusdb/milvus:latest`, CPU) requires the `etcd` sidecar — embedded
etcd was removed in Milvus 2.6.x, so etcd runs as a compose service even
though it is not in the §13 service list. It publishes no ports.

Milvus shares our RustFS for object storage (bucket `sage-milvus`, created by
Milvus on first start under the root credentials).

> **Colocating with new_DRUID on the same box:** the default host ports above
> live in the 8010/9010/19531 block specifically so they do not collide with
> new_DRUID's (gateway 8080, vLLM 8000, document_api 8001, mcp 8002,
> embedding 8003, retrieval 8004, web_ui 8005, minio 9000/9001, milvus
> 19530/9091). Persistent data is isolated in `/DATA/docker/mounts/sage/`
> (preventing collision with new_DRUID's `/DATA/docker/mounts/druid/`).
> If you still need different ports, remap the `${*_HOST_PORT}`
> variables in `.env`. Inter-service URLs use the fixed container ports and
> never need changing.

## Staging model artifacts

The shared vLLM project loads the complete `google/embeddinggemma-2`
checkpoint once from
`/DATA/docker/mounts/models/google/embeddinggemma-2`, including its
`chat_template.jinja`. Both DRUID and SAGE use that registry; configure
`MODEL_DIR` consistently. See [shared-embedding.md](shared-embedding.md).

SAGE retrieval and ingestion still need their MiniLM reranker/tokenizer
under `/DATA/docker/mounts/sage/model-artifacts/minilm`, mounted read-only
at `/artifacts/minilm`. No separate SAGE EmbedGemma copy is needed by the
default adapter. All model serving remains offline-first, with
`HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`.

```bash
sudo mkdir -p /DATA/docker/mounts/sage/model-artifacts/minilm
sudo cp -r /path/to/your/models/minilm/* /DATA/docker/mounts/sage/model-artifacts/minilm/
```

`MINILM_MODEL_PATH` and `MINILM_TOKENIZER_PATH` in `.env` point at the
in-container path `/artifacts/minilm`. `MODEL_PATH` is only needed when
explicitly choosing the optional local `st` embedding backend.

## Host bind-mount directories

All persistent data lives in bind mounts under `/DATA/docker/mounts/sage/<service>`
— no named volumes. Docker creates a missing bind-mount directory as **root**,
so pre-create them and fix ownership for the images that don't run as root:

```bash
sudo mkdir -p /DATA/docker/mounts/sage/{postgres,redis,rustfs,etcd,milvus,model-artifacts}
sudo chown 70:70       /DATA/docker/mounts/sage/postgres   # postgres:16-alpine runs as UID 70
sudo chown 999:999     /DATA/docker/mounts/sage/redis      # redis:7-alpine runs as UID 999
sudo chown 10001:10001 /DATA/docker/mounts/sage/rustfs     # rustfs runs as UID 10001
# etcd and milvus run as root — no chown needed.
```

## First-run order

```bash
cd /path/to/new_sage/deploy

# 0. secrets + any host-port remapping
cp .env.example .env && $EDITOR .env

# Shared inference: start ONCE from either repository, before app services.
docker network inspect embedding-net >/dev/null 2>&1 || docker network create embedding-net
docker compose --env-file .env -f docker-compose.embedding.yml up -d --wait --wait-timeout 600

# 1. base infrastructure
docker compose up -d postgres redis rustfs etcd milvus-standalone

# 2. create the sage_chunks collection + indexes (idempotent; safe to re-run)
docker compose run --rm milvus_setup
# (or: docker compose --profile … no — it runs automatically on `up` too,
#  but running it explicitly shows the provisioning log)

# 3. everything else
docker compose up -d

# 4. seed import — the seed-ingestion agent's scope; it enqueues jobs on the
#    redis queue consumed by ingestion-worker, writing to Milvus + catalog.
```

Verify:

```bash
docker compose run --rm milvus_setup python -m milvus_setup.create_collection --check-only
curl http://localhost:8010/health   # mcp-server (once the service agent lands the app)
```

## Useful commands

```bash
docker compose ps
docker compose logs -f mcp-server retrieval embedding ingestion-worker
docker compose up -d --scale ingestion-worker=4   # more CPU crawl workers
docker compose run --rm milvus_setup python -m milvus_setup.create_collection --recreate --yes  # DATA LOSS: drops sage_chunks
```

## Notes for sibling agents

- The container ports in the service map are the contract; host ports may be
  remapped. Inside `sage-net`, reach services by name: `http://retrieval:8001`,
  `http://embedding:8002`, `http://milvus-standalone:19530`.
- `mcp-server` binds `0.0.0.0:8000` inside its container (see `MCP_HOST` /
  `MCP_PORT` in the compose file). No auth — host firewall/Tailscale is the
  boundary (spec §13).
- The `embedding`, `retrieval`, `mcp_server`, and `ingestion` Dockerfiles are
  owned by their respective build agents; the compose file references their
  expected paths (`../services/<name>/Dockerfile`).
- The `milvus_setup` one-shot image is complete: `../services/milvus_setup/`
  builds, provisions `sage_chunks` per spec §9.1, and exits.
