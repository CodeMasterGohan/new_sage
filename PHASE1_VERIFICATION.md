# New_sage Phase 1: verification and rollout

These changes introduce a PostgreSQL-backed MCP resolver (refreshed at TTL),
a query-docs compatibility tool while retaining get-library-docs,
corrected version fallback semantics and an opt-in strict mode, optional
on-demand RQ crawl dispatch, a cached offline tokenizer, and safer Compose
defaults for host bindings, secrets, and network membership.

## Safe local verification

Before deployment, review the pull request and its CI status. Then:

```bash
python -m compileall -q services tests-e2e
PYTHONPATH=.:services/mcp_server/src pytest -q \
  services/mcp_server/tests services/retrieval/tests/test_catalog_sql.py tests-e2e
cp deploy/.env.example deploy/.env
docker compose -f deploy/docker-compose.yml config --quiet
```

The CI suite uses fakes. Successful CI does **not** verify live Milvus,
PostgreSQL, Redis, model weights, or container networking.

## Deployment precautions

1. Back up PostgreSQL, Milvus, etcd, Redis, and RustFS data.
2. Set unique, non-placeholder `POSTGRES_PASSWORD` and `RUSTFS_SECRET_KEY`
   in `deploy/.env` and review all model artifact paths.
3. Review and test the pinned `MILVUS_IMAGE` against the running index.
   Do not upgrade a database image without verifying compatibility.
4. Set `MCP_BIND_IP` to loopback (default) or an explicit trusted Tailscale
   interface IP. The MCP server has **no application-level authentication**.
5. Keep `SAGE_AUTO_CRAWL=0` unless on-demand remote source crawling is
   approved. An enabled crawl only targets already registered libraries.
6. Start services using the documented catalog migrations and collection
   initialization steps.

## Live H200 acceptance checklist

- Check catalog-backed library resolution and refresh after registering a
  new library without restarting the MCP server.
- Call the legacy `get-library-docs` and new `query-docs` tools through a
  real MCP client. Verify output contracts and provenance.
- Request an unindexed explicit version: inspect `version_fallback`, and
  verify `strict_version=true` yields no wrong-version passages.
- If auto-crawl is enabled, check RQ enqueue/dedup, actual crawler execution,
  ingestion completion and subsequent retrieval.
- Check that PostgreSQL/Milvus/RustFS/Redis remain accessible to the
  corresponding services on the private Compose network while backend
  services have no `cora` network membership.
- Verify clean-checkout ingestion image build and mount the offline MiniLM
  tokenizer at /artifacts/minilm for BOTH ingestion-worker and crawler-worker.
- Verify no crawler job uses a null catalog writer or reports partial Milvus
  upserts as successful.
- Check model load, source citations, response quality, p95 query latency,
  and restart/recovery behavior before deployment.

## Limitations

- The `query-docs` adapter accepts Context7-style arguments for known
  GitHub-style IDs; the payload remains New_sage's provenance-rich format,
  not a guaranteed wire-identical Context7 implementation.
- The MCP resolver periodically refreshes synchronously; a larger catalog
  should use a more efficient incremental or asynchronous refresh.
- Git-repository imports still need file/commit/line-level provenance.
- CI does not contain a live infrastructure test; keep rollout gated on
  the acceptance checks above.
