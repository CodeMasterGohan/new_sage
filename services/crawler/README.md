# New_sage crawler service

On-demand versioned-docs crawler (spec §5.2, §6, §9.3.1, §12). When
`get-library-docs` hits an unknown or stale (library, version), a crawl
job is enqueued instead of failing; the job fetches the docs politely,
snapshots raw HTML to RustFS, and feeds the ingestion pipeline, which
marks the catalog version ready.

## Layout

| File | Purpose |
|---|---|
| `polite_fetch.py` | `PoliteFetcher.fetch(url) -> (html, final_url)`: robots.txt obeyed (fail closed on disallow), 1 req/s per host (in-memory timestamps, thread-safe), exponential backoff on 429/5xx (max 4 retries, honors `Retry-After`), User-Agent `NewSageBot/1.0 (+personal research crawler; contact: local)`. HTTP layer (`http_get`) and clock (`sleep`/`monotonic`) injectable |
| `version_detect.py` | Strategies tried in order by `detect(library_record)`: `sphinx_versions_json` (version→URL map), `docusaurus_versions` (versions.json, then version-dropdown scrape), `latest_redirect` (`/latest/` → final URL), `git_tags` (`git ls-remote --tags`, no clone). Each returns `None` when not applicable; network/git runners injectable |
| `repo_clone.py` | `clone_at_tag(repo_url, tag, dest)` → commit SHA (`git clone --branch <tag> --depth 1` via subprocess, no GitPython; tags validated against option/path injection); `find_doc_files(checkout)` → README.md + `docs/**/*.md` + CHANGELOG.md (case-insensitive, dot-dirs skipped, sorted); `pages_from_clone` hands the list to `services.ingestion.importers.import_markdown_files`, matching its `Page` shape exactly |
| `snapshots.py` | `SnapshotStore` ABC; `RustFSSnapshotStore` (real, `minio` package against RustFS's S3-compatible endpoint) and `InMemorySnapshotStore` (test fake). `put_html(key, html)` → key for `page.raw_snapshot_key`. Secrets from `RUSTFS_*` env only — never in code |
| `stale.py` | `needs_refresh(row)`: `crawled_at + refresh_after_days < now`, ready-only, naive-datetime-safe — the same predicate as `CatalogRepository.list_stale_versions`, so the two can never disagree |
| `jobs.py` | RQ job functions (see below), the sitemap/page-list discovery, and `RepoCatalogWriter` (adapts the ingestion pipeline's `CatalogWriter` protocol onto the real `CatalogRepository`: page rows with `raw_snapshot_key`, chunk rows routed by content-hash, `mark_version_ready`) |
| `devdocs_manifest.py` | Monthly DevDocs sync manifest plumbing (§5.1.1): strict YAML-subset parser for the approved seed manifest (no PyYAML in this image), GHCR digest re-resolution (metadata only, stdlib urllib), docker create/cp/rm image staging, `{docset: version}` from each docset's `db.json` mtime (content-hash fallback). All failures are `DevDocsManifestError` with actionable messages |
| `worker.py` | RQ worker consuming the `sage-crawl` queue (`REDIS_URL`) |

## Job functions

```python
from services.crawler.jobs import enqueue_crawl
enqueue_crawl(library_id, version)   # -> RQ job id on the sage-crawl queue
```

- **`crawl_library_version(library_id, version, *, make_latest=False)`** —
  repo-clone is preferred when the library record has a `repo_url`
  (§9.3.1): match the version to a git tag (exact, `v`-prefixed, or
  `v`-stripped), `clone_at_tag`, ingest Markdown directly
  (`source="repoclone"`), commit SHA returned in the job report.
  Otherwise web crawl (§5.2): version detection → sitemap (one-level
  index recursion, §6 scope filter: same origin + base path prefix,
  5000-URL safety cap) → polite fetch → RustFS snapshot → HTML→Markdown
  → `pipeline.ingest_pages(source="crawl")` → catalog marked ready.
  Cross-version dedup (§6) passes the current latest ready version as
  `prev_version`. Failures mark the version row `failed` and the
  `crawl_job` row `failed` with the error.
- **`refresh_stale()`** — §12 sweep: `list_stale_versions()` →
  one RQ crawl per stale version. Runs on the **`scheduler` compose
  service** (`services/scheduler/`, weekly by default via
  `REFRESH_INTERVAL_SECONDS`), which calls this exact entry point
  in-process — the same function an RQ worker would run.
- **`devdocs_sync(manifest=None)`** — §5.1.1 monthly sync. With an
  explicit `{docset: version}` manifest it diffs + ingests directly
  (tests do this). Without one — the production path — it loads the
  approved seed manifest (`services/artifacts/manifests/seed-corpus.yaml`,
  APPROVAL-2026-101; `DEVDOCS_MANIFEST_PATH` overrides), re-resolves the
  image digest via registry metadata, pulls the resolved digest
  (`docker pull`, the one explicitly approved network fetch), stages the
  docset tree out of the image (`docker create`/`cp`/`rm` — the container
  is never executed), builds `{docset: version}` from each docset's
  `db.json` mtime, and diffs against the catalog. The **diff**
  (`devdocs_diff`, pure logic, no docker) classifies each docset as
  `changed` / `unchanged` / `added` / `removed` and re-ingests only
  changed+added (unchanged: no re-chunk, no re-embed). Removed docsets are
  never deleted — the catalog is append-only (§4), so they stay searchable;
  removal is detected via the `devdocs` ecosystem marker the sync stamps on
  every library it creates or ingests (other seed sources like MDN/tldr are
  never flagged). A `crawl_job` row of kind `devdocs_sync` records the run
  durably (failed runs keep the error). `enqueue_devdocs_sync()` puts one
  sync job on the `sage-crawl` queue — that is what the monthly
  `devdocs-scheduler` calls; the scheduler never pulls or ingests itself.

All three have `*_impl` cores with injectable seams
(repo/fetcher/snapshots/ingest/git-runner/extractor/importer) — tests
exercise the real `CatalogRepository` (SQLite) with fakes everywhere else.

## Environment

| Variable | Required | Purpose |
|---|---|---|
| `REDIS_URL` | yes | RQ transport (`sage-crawl` queue) |
| `DATABASE_URL` | yes | catalog Postgres (`postgresql+psycopg://…`) |
| `RUSTFS_ENDPOINT` | yes | snapshot store |
| `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY` | for real RustFS | snapshot store auth |
| `RUSTFS_BUCKET` | no (default `sage-snapshots`) | snapshot bucket |
| `RUSTFS_SECURE` | no (default `false`) | TLS to RustFS |
| `DEVDOCS_MANIFEST_PATH` | no (default `services/artifacts/manifests/seed-corpus.yaml` next to the package; the crawler-worker image carries it at `/app/services/artifacts/manifests/seed-corpus.yaml`) | approved seed manifest (APPROVAL-2026-101) the monthly `devdocs_sync` reads its digest-pinned image reference from |
| `DEVDOCS_IMAGE_DOCS_PATH` | no (default `/app/public/docs`) | docset tree location *inside* the DevDocs image; assumption to verify against the real image on first production run |

## Run

```bash
# worker
REDIS_URL=redis://redis:6379/0 DATABASE_URL=postgresql+psycopg://sage:sage@postgres:5432/sage \
  RUSTFS_ENDPOINT=rustfs:9000 python -m services.crawler.worker

# tests (from ~/workspace/new_sage/build; fully offline)
./services/crawler/.venv/bin/python -m pytest services/crawler/tests -q
```

The Docker image builds from the build root so `services.catalog` and
`services.ingestion` are importable siblings (same pattern as the
ingestion image); `git` is installed in the image for repo-clone pinning.

## Deviations from the task/spec

1. **Re-crawls update the existing version row in place** instead of
   creating new rows. The catalog's `UNIQUE(library_id, version)`
   constraint makes "new row per re-crawl" unimplementable for the same
   version string; `get_or_create_version` idempotency + `mark_version_ready`
   (which re-stamps `crawled_at`) gives the same observable behavior for
   the stale-while-revalidate sweep.
2. **robots.txt fetch failure fails open** (allows the fetch) while a
   robots *disallow* fails closed — the task's "fail closed on disallow"
   read literally. Standard crawler behavior; the disallow case raises
   `RobotsDisallowed` before any page request.
3. **`enqueue_crawl` lives in `jobs.py`, not `worker.py`** (unlike the
   ingestion service's `enqueue_ingest`), to avoid a circular import:
   `refresh_stale` needs it and `worker.py` imports from `jobs.py`.
4. **Repo-clone snapshots record the commit SHA in the job report** rather
   than a RustFS tarball of the checkout (spec §9.3.1 allows "just record
   the commit SHA; the clone is reproducible from the tag"). A tarball
   snapshot is listed under Gaps.
5. **Version lifecycle statuses** walk `crawling`/`cloning` → `parsing` →
   `ready`; the fine-grained `chunking`/`embedding`/`indexing` steps are
   owned by the ingestion pipeline internally and are not mirrored to the
   catalog row per stage.

## Gaps / future work

- `devdocs_sync` production assumptions to verify on the H200's first
  monthly run: the docset tree path inside the real image
  (`DEVDOCS_IMAGE_DOCS_PATH`, default `/app/public/docs`) and the
  `db.json` `mtime` version-stamp convention. The sync fails closed with
  clear errors if either is wrong — nothing is ingested on a guess.
- The crawler-worker container needs the docker socket mounted for the
  sync's `docker pull`/`create`/`cp`/`rm` (not in the checked-in compose
  file — a deliberate deploy-time decision for James).
- Commit SHAs from repo-clone crawls live in the RQ job report only —
  there is no catalog column for them yet (would need an Alembic
  migration; flagged, not silently dropped).
- No per-library crawl concurrency cap beyond the per-host 1 req/s
  throttle; a very large sitemap (5000 URLs) is one long sequential job.
- RustFS tarball snapshots of repo checkouts (see deviation 4).
