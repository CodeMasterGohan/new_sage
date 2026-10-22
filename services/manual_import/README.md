# Manual documentation imports

This is an optional **administrator-only** upload API, compact web form, and
CLI for local documentation that is not present in DevDocs, MDN or a tagged
repository. SAGE's normal MCP read path is unaffected.

## Setup

Add a secret of at least 32 characters to `deploy/.env`:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Set `SAGE_ADMIN_TOKEN=<generated value>` (do not commit this value). Then:

```bash
docker compose -f deploy/docker-compose.yml --profile admin up -d --build admin-api crawler-worker
```

The admin service listens only on `127.0.0.1:8012` by default. If it must
be accessed from a different host, tunnel it over SSH or terminate HTTPS
with authentication at a trusted reverse proxy. **Never publish the upload
endpoint as unauthenticated HTTP.** The existing MCP service has no auth;
admin uploads must not be routed through it.

### Browser

Visit `http://127.0.0.1:8012/admin`. Supply the admin token, a library
slug (`gitlab`), a version (`18.4`), and one ZIP or multiple `.md`,
`.markdown`, `.html`, `.htm` files. For nested folders, use a ZIP archive
so relative paths survive. The page polls until ingestion completes and
displays its result.

### CLI

Run from the repository root, on a machine that can reach the admin API.
The CLI uses only Python standard-library HTTP/ZIP modules:

```bash
export SAGE_ADMIN_TOKEN='your-long-secret-from-deploy-env'
python3 -m services.manual_import.cli /path/to/gitlab-docs \
  --library gitlab --version 18.4 --url http://127.0.0.1:8012 --wait
```

The path may be a single Markdown/HTML file, a ZIP, or a directory; directory
mode includes supported files recursively. Avoid putting the secret directly
in command-line arguments or shell history.

### REST API

```bash
curl -fsS -X POST http://127.0.0.1:8012/admin/imports \
  -H "Authorization: Bearer $SAGE_ADMIN_TOKEN" \
  -F 'library=gitlab' -F 'version=18.4' \
  -F 'files=@/path/to/gitlab-docs.zip'
```

Returns HTTP 202 with `job_id`, `status=queued` and a `status_url`.

```bash
curl -fsS "http://127.0.0.1:8012/admin/imports/<job-id>" \
  -H "Authorization: Bearer $SAGE_ADMIN_TOKEN"
```

Status is `queued`, `started`, `finished`, or `failed` (plus RQ's other
terminal statuses). A finished job includes page and chunk counts. Error
details remain in the crawler-worker logs; they are not exposed to clients.

## Behavior and limitations

- Source is `manual`, a new `VersionSource`. On each successful upload,
  SAGE publishes a **new immutable revision** for `(library, version)`.
  Earlier READY revisions remain until superseded, and stale vectors are
  pruned after a successful replacement.
- Original ZIP and original files are preserved in RustFS under a unique
  upload identifier; catalog pages point to original snapshots.
- HTML uses SAGE's `html_to_markdown`, Markdown bypasses conversion. The
  same heading-aware chunker, embedding service and Milvus hybrid index
  used by repository crawling are reused.
- Links to local documents use `sage://manual/library/version/path`, not
  fabricated upstream URLs. Preserve filenames for reliable provenance.
- Manual versions are excluded from `refresh_stale`. Upload another ZIP
  to update them; the ZIP represents the complete replacement docset for
  that library/version, not an incremental patch.
- One upload: at most 64 MiB compressed, 128 MiB expanded, 1,000 supported
  documents, and 8 MiB per document. Unsafe paths, symlinks, duplicates,
  empty archives and encrypted ZIPs are rejected. Non-document assets in
  ZIPs are ignored.
- The admin UI is a small built-in form; it doesn't introduce a new
  frontend framework or alter the MCP tool contract.
- The queue uses the existing `sage-crawl` worker; rebuild that image when
  enabling manual imports. If the worker is stopped, uploads remain queued.
- No browsing or external downloads are involved in manual ingestion.
  The SAGE embedding/reranking model artifacts still need to be staged
  locally as usual.

## Testing

```bash
python -m pytest services/manual_import/tests -q
```
