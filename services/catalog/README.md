# New_sage catalog service

Postgres-backed library catalog for New_sage (spec §4, §11). It is the
authority on **what is indexed** (libraries, versioned page sets) and
**what is stale** (crawl jobs, refresh horizons). Single-user: no
tenancy, no auth, no ACLs (spec §13).

## Schema

```
library            id UUID PK
                   name TEXT UNIQUE NOT NULL        -- canonical name
                   aliases TEXT[] NOT NULL DEFAULT '{}'
                   ecosystem TEXT                    -- npm | pypi | crates | ...
                   docs_url TEXT
                   repo_url TEXT
                   resolver_notes TEXT               -- version-detection strategy (§6, §9.3)
                   created_at TIMESTAMPTZ NOT NULL

library_version    id UUID PK
                   library_id UUID FK → library.id (CASCADE) NOT NULL
                   version TEXT NOT NULL
                   is_latest BOOL NOT NULL DEFAULT FALSE   -- §9.2 fallback target
                   crawled_at TIMESTAMPTZ                   -- set when READY
                   source TEXT NOT NULL                    -- seed | crawl | repoclone
                   status TEXT NOT NULL DEFAULT 'pending'  -- → … → ready | failed
                   refresh_after_days INT NOT NULL DEFAULT 30
                   UNIQUE (library_id, version)

page               id UUID PK
                   library_version_id UUID FK → library_version.id (CASCADE) NOT NULL
                   url TEXT
                   title TEXT
                   section_path TEXT
                   raw_snapshot_key TEXT                 -- RustFS key (§5.3)
                   content_hash TEXT                      -- cross-version dedup (§6)

chunk              id UUID PK
                   page_id UUID FK → page.id (CASCADE) NOT NULL
                   ordinal INT NOT NULL
                   text TEXT NOT NULL
                   token_count INT
                   headings TEXT[] NOT NULL DEFAULT '{}'  -- breadcrumb (§15)

crawl_job          id UUID PK
                   library_id UUID FK → library.id (SET NULL)  -- NULL for corpus-wide jobs
                   version TEXT                                -- NULL for corpus-wide jobs
                   kind TEXT NOT NULL        -- crawl | repoclone | devdocs_sync |
                                             -- refresh | seed_ingest
                   status TEXT NOT NULL DEFAULT 'queued'  -- → running → succeeded|failed|cancelled
                   created_at TIMESTAMPTZ NOT NULL
                   finished_at TIMESTAMPTZ
                   error TEXT
```

Version lifecycle: `pending → crawling|cloning → parsing → chunking →
embedding → indexing → ready` (or `failed`). Only `ready` versions are
searchable; re-crawls create new rows so the old index stays live until the
new one is ready (stale-while-revalidate, §6).

## Environment

| Variable       | Required | Example                                             |
|----------------|----------|-----------------------------------------------------|
| `DATABASE_URL` | yes      | `postgresql+psycopg://sage:sage@postgres:5432/sage` |

The Alembic environment refuses to run without `DATABASE_URL` — it never
guesses a database.

## Migrations

From this directory:

```bash
export DATABASE_URL=postgresql+psycopg://sage:sage@localhost:5432/sage
alembic upgrade head        # apply
alembic downgrade -1        # roll back one revision
alembic revision --autogenerate -m "describe change"
```

In the compose stack the `catalog` image runs `alembic upgrade head` and
exits before the other services start.

## Repository API

Import path: `from services.catalog.repository import CatalogRepository`
(also re-exported from `services.catalog`).

```python
repo = CatalogRepository(session)

# libraries — §9.3 resolver
lib = repo.create_library(name="react", aliases=["reactjs"], ecosystem="npm",
                          docs_url="https://react.dev", repo_url="https://github.com/facebook/react")
lib = repo.resolve_library("ReactJS")          # name OR alias, case-insensitive
repo.update_library(lib.id, docs_url="...")     # resolver-discovered fields

# versions
ver, created = repo.get_or_create_version(lib.id, "19.0.0", source="crawl")
repo.update_version_status(ver.id, "crawling")  # pipeline advances
repo.mark_version_ready(ver.id, make_latest=True)
repo.list_versions(lib.id)
repo.get_latest_version(lib.id)
repo.resolve_version(lib.id, "18.3.1")          # → (row, used_fallback) per §9.2
repo.list_ready_versions(lib.id)                # searchable set for retrieval
repo.list_stale_versions()                      # crawled_at + refresh_after_days < now
repo.delete_version(ver.id)                     # prune; pages/chunks cascade

# pages + chunks
page = repo.record_page(ver.id, url="https://react.dev/reference/react",
                        title="React API", content_hash="sha256:…",
                        raw_snapshot_key="snapshots/react/19.0.0/…")
repo.record_chunks(page.id, [
    {"ordinal": 0, "text": "…", "token_count": 312,
     "headings": ["API Reference", "Hooks"]},
])
repo.list_chunks(page.id)                        # ordered by ordinal
repo.count_chunks(ver.id)

# crawl jobs — §5.2 enqueue instead of failing
job = repo.enqueue_job(kind="crawl", library_id=lib.id, version="19.0.0")
repo.update_job_status(job.id, "running")
repo.update_job_status(job.id, "succeeded")     # stamps finished_at
repo.list_pending_jobs()                        # FIFO queue for workers
```

Errors: `services.catalog.errors.NotFoundError` (missing record),
`ConflictError` (duplicate library name).

## Tests

SQLite-backed, no Postgres needed:

```bash
cd ~/workspace/new_sage/build
python -m pytest services/catalog/tests -q
```

Covers: library CRUD + alias/case-insensitive resolution + duplicate
conflict, version get-or-create idempotency, latest flipping, stale
horizons (incl. naive-datetime handling), page/chunk recording + ordering
+ cascade prune, job enqueue/status/finished_at, and field validation.
