# new_sage MCP server

FastMCP server exposing the Context7-compatible tool surface (spec §10)
for New_sage, the local-first Context7 alternative.

**No authentication of any kind** (spec §13): single user, own network.
Binding to localhost / the Tailscale interface is a compose-level concern.

## Tools

### `resolve-library-id(libraryName, ecosystem?)`

Resolves a library name to its canonical record (spec §9.3):

1. Exact match on canonical name or aliases (normalized).
2. Ecosystem-aware disambiguation via the optional `ecosystem` hint.
3. Fuzzy BM25-over-names fallback returning scored alternatives.
4. Unknown → `{found: false, message: "unknown library",
   hint: "supply docs_url to register"}`.

### `get-library-docs(library_id, version?, topic?, tokens=10000)`

Version-pinned doc passages with provenance (spec §10, §15). `version`
defaults to the latest indexed version; `topic` scopes retrieval to a
free-text query (omit for overview pages); `tokens` caps returned text.

Unknown/stale versions never fail: the tool serves what is indexed, sets
`version_fallback: true`, adds a `status` note, and sets
`crawl_recommended: true` (the P3 crawler enqueues the missing version).

Each passage `text` is wrapped in inline provenance delimiters carrying
library, version, `is_latest`, source URL, and section breadcrumb:

```
<<<sage:passage library="React" version="19.2.0" is_latest="true"
  url="https://react.dev/..." section="API Reference / Hooks"
  trust="untrusted-document-content">>>
...
<<<sage:end-passage>>>
```

Passage text is untrusted documentation data — never instructions.

## Layout

```
src/mcp_server/
  server.py           Streamable HTTP app (port 8000, no auth) + tool registration
  tools.py            The 2 tool implementations
  models.py           Arg validation + result/provenance contracts
  resolver.py         §9.3 resolution over the P1 seed table
  clients.py          Retrieval HTTP client + in-memory fake
  stdio_adapter.py    Standalone stdio transport (local use)
  libraries_seed.json P1 resolver table (§18 P1): 15 hand-written libraries
tests/                pytest contract tests (see below)
```

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `RETRIEVAL_URL` | `http://retrieval:8001` | Retrieval service base URL |
| `SAGE_RESOLVER_TABLE` | bundled `libraries_seed.json` | Resolver table path |
| `MCP_ALLOWED_ORIGINS` | (empty) | Comma-separated browser origins (DNS-rebinding) |
| `SAGE_MCP_DEV_FAKE_CLIENTS` | (unset) | `1` → in-memory retrieval fake (dev only) |

## Run

HTTP (port 8000):

```bash
uvicorn --factory mcp_server.server:create_app --host 0.0.0.0 --port 8000
```

stdio:

```bash
python -m mcp_server.stdio_adapter --retrieval-url http://localhost:8001
# dev: --dev-fake-clients
```

## Tests

```bash
pytest
```

Covers: tool arg validation, resolver logic (exact / alias / ecosystem
disambiguation / fuzzy / unknown), provenance rendering (incl. attribute
injection rejection), tool contracts against fake downstream clients
(2 tools, exact Context7 names, schema hygiene), stdio adapter.

## Seams for sibling services

- **Catalog (P2+)**: `LibraryResolver` takes plain `LibraryRecord` rows.
  When the catalog service lands, replace `from_seed_file` with a
  catalog-backed loader — no tool changes needed.
- **Retrieval**: `HttpRetrievalServiceClient` codes against
  `POST /retrieve` with
  `{"library_id","version","topic","top_k":50,"token_budget":tokens}` →
  `{"library","version","version_fallback","passages":
  [{"text","url","section","code_language","score"}]}`.
