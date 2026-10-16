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
docker network create sage-net  # optional: Compose creates it automatically

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
