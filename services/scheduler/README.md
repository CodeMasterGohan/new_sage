# New_sage refresh scheduler

Runs the crawler's §12 stale-while-revalidate sweep on a fixed interval
(spec §17.6: weekly). This is the scheduler the crawler README used to
refer to as the "compose scheduler" — it now exists, as the `scheduler`
compose service.

## What it does

Every `REFRESH_INTERVAL_SECONDS` (default 604800 = 7 days) the scheduler
calls **`services.crawler.jobs.refresh_stale()`** in-process — the exact
same entry point the RQ worker exposes. That function opens the catalog,
finds versions where `crawled_at + refresh_after_days < now`, and enqueues
one `crawl_library_version` job per stale version on the `sage-crawl`
queue. The scheduler never crawls anything itself; the `crawler-worker`
service consumes the queue.

## Layout

| File | Purpose |
|---|---|
| `scheduler.py` | `Scheduler` loop, `SchedulerConfig.from_env()`, `parse_interval_seconds()`. Stdlib only; the crawler import is lazy so unit tests never touch Postgres/Redis |
| `run.py` | CLI: `python -m scheduler.run [--once] [--interval-seconds 2d]` |
| `Dockerfile` | `python:3.11-slim`, offline env (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`); installs the crawler's requirements so the in-process `refresh_stale()` call has its full dependency closure |

## Behavior

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

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `REDIS_URL` | (required) | RQ transport — `refresh_stale()` enqueues crawl jobs |
| `DATABASE_URL` | (required) | catalog Postgres — `refresh_stale()` lists stale versions |
| `REFRESH_INTERVAL_SECONDS` | `604800` (7 days) | sweep interval; plain seconds or `s`/`m`/`h`/`d` suffix (`7d`) |
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

- The monthly `devdocs_sync` job is **not** on this scheduler yet: `devdocs_sync`
  without an explicit manifest raises `NotImplementedError` (real DevDocs
  image-manifest parsing is still an open gap in the crawler), so there is
  nothing schedulable to call. Wire it here once that gap closes.
- `refresh_after_days` (default 30) is a per-library catalog attribute, not a
  scheduler setting — the scheduler only controls *how often the sweep runs*,
  not *what counts as stale*.
