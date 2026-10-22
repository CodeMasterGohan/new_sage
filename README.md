# New_sage — Personal Context7 Alternative

> Version-pinned documentation for your AI coding agent. Ask about any
> library's API; get back accurate, cited docs — never hallucinated
> signatures. **No authentication** (the corpus is public documentation,
> and it's for personal use). Deploys CPU-only to the same H200 as
> new_DRUID.

---

## What it is

[Context7](https://context7.com)-compatible MCP server over a self-hosted
documentation corpus. Two tools, one flow:

```
you: "how do I use React's useOptimistic hook?"
  1. resolve-library-id  →  /react/dev  (exact name match, version-pinned)
  2. get-library-docs    →  ranked passages from the pinned version's docs,
                            with file + section provenance
```

Why self-host instead of using Context7? Version pinning you control, a
corpus you choose (full DevDocs + MDN + tldr), retrieval tuned for code
(420/48 chunking, identifier-boosted hybrid search, MiniLM rerank), and no
third party seeing what you look up.

## Corpus & freshness

| Source | How it stays fresh |
|---|---|
| **DevDocs** (full archive) | Monthly image pulled by digest, diffed against the catalog — only changed docsets re-ingested. Never crawled. |
| **MDN**, **tldr** | Version-pinned checkouts, refreshed by the weekly scheduler |
| **On-demand** | Any library/version: repo cloned at the tag when a repo URL is known, web crawl otherwise — then background-refreshed (stale-while-revalidate) |

- **Immutable revisions**: every re-crawl inserts a new revision row; old
  versions are never mutated, so citations stay stable.
- **Chunking**: 420 tokens / 48 overlap; headings and extracted identifiers
  are folded into the BM25 input for sharper lexical match.
- **Retrieval**: Milvus hybrid — dense (EmbeddingGemma 2, 768-dim) +
  **server-side BM25** + identifier boost → RRF → MiniLM rerank (width 50).

## Architecture

```
AI agent (MCP client)
  │  mcp_server :8010 (host, no auth — Origin check only)
  ▼
┌────────────────────────────────────────────────────────┐
│ retrieval — dense + BM25 + identifiers → RRF → rerank   │
└──────────────┬─────────────────────────────────────────┘
               ▼
   catalog (PostgreSQL: libraries, immutable version revisions)
               ▲
   ┌───────────┴────────────┐
   │ ingestion pipeline     │
   │ crawl → extract →      │
   │ chunk → embed → index  │
   └───────────┬────────────┘
               ▼
   Milvus (sage_chunks) · RustFS (snapshots, seed checkouts)

schedulers (no crawling by hand):
  scheduler ── weekly refresh_stale (stale-while-revalidate)
  devdocs-scheduler ── monthly DevDocs image sync (30d cadence)
```

All containers now use Sage's isolated **`sage-net`** Docker network.
No service joins the external `cora` network by default. The MCP server
has no application-level authentication and is bound to localhost by default.
Only **one** host port is published: **8010** (the MCP server). Everything
else is container-to-container.

## Quickstart

```bash
cp deploy/.env.example deploy/.env
# Compose creates sage-net automatically; no shared network is required.

# 1. Infrastructure + migrations + seed the catalog
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml run --rm catalog-migrate
docker compose -f deploy/docker-compose.yml run --rm milvus_setup
# 2. Seed corpus (DevDocs image + MDN + tldr, per APPROVAL-2026-101)
#    The first devdocs_sync does a catch-up ingest for every docset
#    without a READY row; MDN/tldr are enqueued as version-pinned crawls.
docker compose -f deploy/docker-compose.yml run --rm scheduler \
  python -m scheduler.run --once --job devdocs_sync
# 3. Everything else (schedulers included)
docker compose -f deploy/docker-compose.yml up -d
```

Then point your agent at it:

```jsonc
// Claude Code / MCP client config
{ "mcpServers": { "sage": {
  "url": "http://127.0.0.1:8010/mcp"
  // …or stdio: python -m mcp_server.stdio_adapter
  //   --retrieval-url http://<h200>:8011
}}}
```

## MCP tools

| Tool | Arguments | Returns |
|---|---|---|
| `resolve-library-id` | `libraryName`, `ecosystem?` | Canonical `/library/version` ID (+ close alternatives) |
| `get-library-docs` | `libraryID`, `query?`, `topic?`, `tokens?` | Ranked passages with file/section provenance |

Names and shapes are Context7-compatible, so prompts and agent configs
written for Context7 work unchanged.

## Manually import Markdown / HTML (admin)

SAGE includes an **optional authenticated admin upload service** for
documentation missing from DevDocs or the regular crawler (for example,
an exported GitLab manual). The existing MCP endpoint remains unchanged and
unauthenticated on localhost; **uploads are only accepted by the separate
admin service** with a bearer token. See
[`services/manual_import/README.md`](services/manual_import/README.md) for
API, CLI, ZIP constraints, and operational details.

1. Generate a strong token and set `SAGE_ADMIN_TOKEN` in `deploy/.env`.
2. Start the service using `docker compose -f deploy/docker-compose.yml --profile admin up -d --build admin-api crawler-worker`.
3. Open `http://127.0.0.1:8012/admin` on the SAGE host, enter the token,
   library (e.g. `gitlab`), version (e.g. `18.4`), and upload one ZIP or
   Markdown/HTML files.

Alternatively use the CLI from the repo root, with `SAGE_ADMIN_TOKEN` in the
environment:

```bash
python -m services.manual_import.cli /path/to/gitlab-docs \
  --library gitlab --version 18.4 --wait
```

Both paths enqueue a background `sage-crawl` worker job that snapshots
original files to RustFS, reuses SAGE's Markdown/HTML extraction and
embedding pipeline, and publishes a new immutable revision when ready.
Manual revisions are excluded from scheduled recrawls.

## Configuration

Highlights from `deploy/.env.example`:

| Variable | Default | Purpose |
|---|---|---|
| `MCP_HOST_PORT` | `8010` | The only published host port |
| `REFRESH_INTERVAL_SECONDS` | `7d` | Weekly refresh cadence |
| `RERANK_WIDTH` | `50` | MiniLM rerank depth |
| `CHUNK_TOKENS` / `CHUNK_OVERLAP` | `420` / `48` | Structural chunking |

## Repo layout

```
services/
  mcp_server/   resolve-library-id + get-library-docs (Streamable HTTP + stdio)
  crawler/      On-demand crawl, DevDocs image sync, seed ingestion, snapshots
  ingestion/    Extract → chunk (420/48) → embed → index pipeline
  embedding/    EmbeddingGemma 2 CPU service (shared prefix contract)
  retrieval/    Hybrid search + RRF + MiniLM rerank (CPU)
  catalog/      PostgreSQL: libraries, immutable version revisions, Alembic
  scheduler/    Weekly refresh_stale (stdlib-only, jittered, --once trigger)
  milvus_setup/ One-shot sage_chunks collection/index job
  artifacts/    Manifest tooling; seed-corpus.yaml (APPROVAL-2026-101)
  eval/         50-question starter eval (target ~150 on real corpus)
deploy/         docker-compose.yml · .env.example · README.md
```

## Offline-first

Like its sibling new_DRUID: pre-staged artifacts, pinned manifests with
sha256 (`services/artifacts/manifests/seed-corpus.yaml`), no downloads at
runtime, no unapproved endpoints. Object storage is **RustFS**
(`rustfs/rustfs:1.0.1`, Apache-2.0).

## Optional Rust document preprocessing

The [Rust Markdown fast path](docs/rust-fastpath.md) offers optional,
Python-compatible heading/fenced-code section scanning and whitespace
normalization after Python HTML extraction. Initial CI microbenchmarks
showed approximately **2.6× faster section parsing** and **4.9× faster
normalization** on synthetic long Markdown. It does **not** change
robots.txt, crawling rate, text token budgets, embedding, or search.

The Python path remains the default; to test the faster implementation,
build/install the pinned PyO3 wheel into **both** crawler and ingestion
worker images and set `SAGE_RUST_FASTPATH=1`. Production rollout still
requires a real-corpus end-to-end ingest benchmark and verification of
unchanged chunk IDs.

## GitLab CI/CD

The checked-in [`.gitlab-ci.yml`](.gitlab-ci.yml) covers contracts,
GitLab runner validation, advisory source/dependency/secret/security scans,
Docker image builds and registry publishing, immutable release manifests,
manual staging/production migrations and deployments, rollback, optional
per-image vulnerability and CycloneDX SBOM reports, and private smoke tests.
See [`.gitlab/README.md`](.gitlab/README.md) for **environment-specific
CI variables**, runner/SSH/registry requirements, and the explicit release
approval sequence. Nothing deploys until the GitLab project is configured
and a human starts its manual jobs. This repository's GitHub Actions only
validate the GitLab YAML/Compose contract; production GitLab CI lint must
still run against the actual GitLab instance.

## Testing

```bash
python -m pytest            # whole tree: 461 passed, 2 skipped
```

Per-service suites run from each `services/<name>/` (own venvs).

## Docs & skill

- 📖 **Full documentation site:** shipped in the **new_DRUID** repo under
  `docs-site/` (18 pages covering both projects — architecture, MCP
  reference, deployment, operations…)
- 🤖 **`druid-sage` skill**: lets any AI answer usage questions about
  New_sage (and new_DRUID) from the real docs.

## Status

Built and tested; all pushes current on `main`. What still needs the H200:
staging EmbeddingGemma 2 + MiniLM weights, the full DevDocs/MDN/tldr
ingestion, live infra tests, and growing the eval set to ~150 questions for
calibration. See `BUILD_LOG.md` for the full build history.
