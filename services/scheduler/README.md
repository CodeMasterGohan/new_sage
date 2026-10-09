# New_sage scheduler

Runs one of two crawler jobs on a fixed interval (spec §12, §17.6):

- **`refresh_stale`** (default): the §12 stale-while-revalidate sweep,
  weekly. This is the scheduler the crawler README used to refer to as
  the "compose scheduler" — it now exists, as the `scheduler` compose
  service.
- **`devdocs_sync`**: the monthly DevDocs image sync (§5.1.1), as the
  `devdocs-scheduler` compose service — the same image, selected with
  `SCHEDULER_JOB=devdocs_sync` and a 30-day interval.

## What it does

Every `REFRESH_INTERVAL_SECONDS` the scheduler calls its configured job
in-process:

- `refresh_stale` → **`services.crawler.jobs.refresh_stale()`** — the
  exact same entry point the RQ worker exposes. That function opens the
  catalog, finds versions where `crawled_at + refresh_after_days < now`,
  and enqueues one `crawl_library_version` job per stale version on the
  `sage-crawl` queue. The scheduler never crawls anything itself; the
  `crawler-worker` service consumes the queue.
- `devdocs_sync` → **`services.crawler.jobs.enqueue_devdocs_sync()`** —
  puts one `devdocs_sync` RQ job on the `sage-crawl` queue. The
  crawler-worker then runs the production sync: load the approved seed
  manifest, re-resolve the image digest, `docker pull`, stage the docset
  tree, diff versions against the catalog, and ingest only changed/added
  docsets. The scheduler never touches docker or the image itself.

## Layout

| File | Purpose |
|---|---|
| `scheduler.py` | `Scheduler` loop, `SchedulerConfig.from_env()` (incl. `SCHEDULER_JOB`), `parse_interval_seconds()`, `JOB_REFRESHERS` registry (`refresh_stale` → `default_refresh`, `devdocs_sync` → `default_devdocs_sync`), `DEVDOCS_SYNC_INTERVAL_SECONDS` (30 days). Stdlib only; the crawler imports are lazy so unit tests never touch Postgres/Redis |
| `run.py` | CLI: `python -m scheduler.run [--once] [--job {refresh_stale,devdocs_sync}] [--interval-seconds 2d]` |
| `Dockerfile` | `python:3.11-slim`, offline env (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`); installs the crawler's requirements so the in-process job calls have their full dependency closure |

## Behavior

- **Job selection**: `SCHEDULER_JOB` (or `--job`) picks `refresh_stale`
  (default) or `devdocs_sync`. The `Scheduler` class itself is
  job-agnostic — it runs whatever zero-arg callable `JOB_REFRESHERS`
  maps the configured job to. A second compose service
  (`devdocs-scheduler`) runs this same image with
  `SCHEDULER_JOB=devdocs_sync` and `REFRESH_INTERVAL_SECONDS=2592000`
  (30 days, `DEVDOCS_SYNC_INTERVAL_SECONDS`) for the monthly cadence —
  no code fork, just config.
- **Startup jitter**: sleeps a uniform random `0 … SCHEDULER_STARTUP_JITTER_SECONDS`
  (default 600s) before the first run, so a stack restart doesn't stampede the queue.
- **Structured logging**: every run logs `refresh started run_id=…` and either
  `refresh finished run_id=… enqueued=N elapsed=…s` or
  `refresh failed run_id=… error=… failures=N elapsed=…s`.
- **Failure backoff, never crash**: a failed sweep logs the error, increments
  the failure counter, and waits `interval × 2^failures` (capped at
  `8 × interval`) before the next attempt. A successful sweep resets the counter.
- **Manual trigger**: `python -m scheduler.run --once` runs one sweep and exits
  `0` (ok) / `1` (sweep failed) / `2` (bad config), printing a JSON summary.
  In compose: `docker compose run --rm scheduler python -m scheduler.run --once`.
  For the monthly sync: `docker compose run --rm devdocs-scheduler python -m scheduler.run --once --job devdocs_sync`.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `REDIS_URL` | (required) | RQ transport — `refresh_stale()` enqueues crawl jobs; `devdocs_sync` enqueues the sync job |
| `DATABASE_URL` | (required for `refresh_stale`) | catalog Postgres — `refresh_stale()` lists stale versions (`devdocs_sync` doesn't need it) |
| `SCHEDULER_JOB` | `refresh_stale` | which job this instance runs: `refresh_stale` or `devdocs_sync` |
| `REFRESH_INTERVAL_SECONDS` | `604800` (7 days) | job interval; plain seconds or `s`/`m`/`h`/`d` suffix (`7d`). The `devdocs-scheduler` compose service sets `2592000` (30 days) |
| `SCHEDULER_STARTUP_JITTER_SECONDS` | `600` | max uniform startup delay; `0` disables |
| `SCHEDULER_BACKOFF_CAP_MULTIPLIER` | `8` | failure backoff capped at this × interval |
| `SCHEDULER_LOG_LEVEL` | `INFO` | log level |

## Run

```bash
# daemon (compose default CMD)
python -m scheduler.run

# single sweep, e.g. after deploying the stack
REDIS_URL=redis://redis:6379/0 DATABASE_URL=postgresql+psycopg://sage:sage@postgres:5432/sage \
  python -m scheduler.run --once

# tests (from ~/workspace/new_sage/build; fully offline, refresh call mocked)
./services/crawler/.venv/bin/python -m pytest services/scheduler/tests -q
```

## Notes / gaps

- `refresh_after_days` (default 30) is a per-library catalog attribute, not a
  scheduler setting — the scheduler only controls *how often the sweep runs*,
  not *what counts as stale*.
- The monthly `devdocs-scheduler` needs the docker socket mounted into
  `crawler-worker` (the worker runs `docker pull`/`create`/`cp`/`rm` for the
  image staging) — wire that in compose on the H200 deploy; it is not in the
  checked-in compose file yet.
