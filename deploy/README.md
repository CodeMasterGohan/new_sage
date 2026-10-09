# New_sage deploy

CPU-only, single-`docker compose` stack for the local-first Context7
alternative (spec §13). **No auth, no GPU.** All services speak plain HTTP
on `sage-net`; the host firewall / Tailscale interface is the security
boundary. Do not expose these ports to the public internet.

## Service map

| Service | Container port | Default host port | What it is |
|---|---|---|---|
| `mcp-server` | 8000 | `${MCP_HOST_PORT:-8000}` | FastMCP: `resolve-library-id`, `get-library-docs` (spec §10) |
| `retrieval` | 8001 | `${RETRIEVAL_HOST_PORT:-8001}` | RRF hybrid (dense+BM25+identifier) + MiniLM rerank (spec §9) |
| `embedding` | 8002 | `${EMBEDDING_HOST_PORT:-8002}` | EmbeddingGemma 2, CPU-served, 768-dim (spec §8) |
| `ingestion-worker` | — | — | RQ worker: crawl → trafilatura → chunk → embed → index (spec §5) |
| `milvus-standalone` | 19530 / 9091 | `${MILVUS_HOST_PORT:-19530}` / `${MILVUS_MGMT_HOST_PORT:-9091}` | `sage_chunks` collection (spec §9.1) |
| `minio` | 9000 / 9001 | `${MINIO_HOST_PORT:-9000}` / `${MINIO_CONSOLE_HOST_PORT:-9001}` | raw snapshots + seed checkouts (spec §11) |
| `postgres` | 5432 | `${POSTGRES_HOST_PORT:-5432}` | catalog: libraries, versions, pages, jobs (spec §11) |
| `redis` | 6379 | `${REDIS_HOST_PORT:-6379}` | crawl queue + hot-response cache (spec §11) |
| `etcd` | — | — | internal only; required by milvus-standalone |

One network (`sage-net`), named volumes: `pgdata`, `redisdata`,
`miniodata`, `etcddata`, `milvusdata`, `model-artifacts`.

Milvus (`milvusdb/milvus:latest`, CPU) requires the `etcd` sidecar — embedded
etcd was removed in Milvus 2.6.x, so etcd runs as a compose service even
though it is not in the §13 service list. It publishes no ports.

Milvus shares our MinIO for object storage (bucket `sage-milvus`, created by
Milvus on first start under the root credentials).

> **Colocating with new_DRUID on the same box:** both stacks publish the same
> default host ports. Copy `.env.example` to `.env` and remap the
> `${*_HOST_PORT}` variables for one of the stacks so they do not collide.
> Inter-service URLs use the fixed container ports and never need changing.

## Staging model artifacts

Services are offline-first (spec §14): they load pre-staged artifacts from
the `model-artifacts` volume and fail startup rather than downloading at
runtime (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` are set in the stack).

Stage once before first boot:

```bash
# create the volume
docker volume create new_sage_model-artifacts   # actual name: <project>_model-artifacts; check `docker volume ls`

# copy the models in (volume mount path is /artifacts inside containers)
docker run --rm \
  -v <volume-name>:/artifacts \
  -v /path/to/your/models:/src:ro \
  alpine sh -c 'mkdir -p /artifacts/embeddinggemma-2 /artifacts/minilm && cp -r /src/embeddinggemma-2/* /artifacts/embeddinggemma-2/ && cp -r /src/minilm/* /artifacts/minilm/'
```

Expected layout in the volume:

```
/artifacts/
  embeddinggemma-2/   # google/embeddinggemma-2 checkpoint, offline loadable
  minilm/             # MiniLM reranker checkpoint + tokenizer
```

`MODEL_PATH`, `MINILM_MODEL_PATH`, `MINILM_TOKENIZER_PATH` in `.env` point at
these paths (defaults shown above).

## First-run order

```bash
cd ~/workspace/new_sage/build/deploy

# 0. secrets + any host-port remapping
cp .env.example .env && $EDITOR .env

# 1. base infrastructure
docker compose up -d postgres redis minio etcd milvus-standalone

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
curl http://localhost:8000/health   # mcp-server (once the service agent lands the app)
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
