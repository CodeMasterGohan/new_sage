"""Weekly refresh scheduler for New_sage (spec §12, §17.6).

Runs one of two jobs on a fixed interval, in-process — no invented APIs,
no shell-outs:

- ``refresh_stale`` (default): the crawler's real ``refresh_stale()``
  entry point (``services.crawler.jobs.refresh_stale() -> list[dict]``),
  once per ``REFRESH_INTERVAL_SECONDS`` (default 7 days).
- ``devdocs_sync``: the monthly DevDocs image sync (§5.1.1) via
  ``services.crawler.jobs.enqueue_devdocs_sync()``, which puts one
  ``devdocs_sync`` RQ job on the ``sage-crawl`` queue for the
  crawler-worker to run. Selected with ``SCHEDULER_JOB=devdocs_sync``
  (or ``--job``); the monthly cadence itself is a second compose service
  (``devdocs-scheduler``) running this same image with
  ``REFRESH_INTERVAL_SECONDS=2592000``.

Design notes
------------
- Stdlib only. The crawler imports are **lazy** (inside
  :func:`default_refresh` / :func:`default_devdocs_sync`) so this module
  imports cleanly without the crawler's third-party dependencies — unit
  tests mock the refresh call and never touch Postgres/Redis.
- A failed run never crashes the loop: the error is logged, the failure
  counter increments, and the next wait is backed off exponentially
  (capped). A successful run resets the counter.
- ``python -m scheduler.run --once`` (or ``python -m services.scheduler.run
  --once``) performs a single sweep and exits 0/1; add
  ``--job devdocs_sync`` to trigger the monthly sync instead.

Environment
-----------
``SCHEDULER_JOB`` (default ``refresh_stale``): which job this scheduler
instance runs — ``refresh_stale`` or ``devdocs_sync``.
``REFRESH_INTERVAL_SECONDS`` (default 604800 = 7 days), plain seconds or
with an ``s``/``m``/``h``/``d`` suffix (``7d``). ``SCHEDULER_STARTUP_JITTER_SECONDS``
(default 600) caps the uniform startup delay before the first run.
``SCHEDULER_LOG_LEVEL`` (default ``INFO``).
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("sage.scheduler")

DEFAULT_INTERVAL_SECONDS = 7 * 24 * 3600  # weekly, per §17.6
#: Monthly cadence for the devdocs_sync compose service (§5.1.1, §17.6).
DEVDOCS_SYNC_INTERVAL_SECONDS = 30 * 24 * 3600
DEFAULT_STARTUP_JITTER_SECONDS = 600  # up to 10 minutes
DEFAULT_BACKOFF_CAP_MULTIPLIER = 8  # backed-off wait never exceeds 8× interval

_SUFFIX_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_interval_seconds(
    raw: Any,
    *,
    default: int = DEFAULT_INTERVAL_SECONDS,
    allow_zero: bool = False,
) -> int:
    """Parse an interval env value into whole seconds.

    Accepts ``None``/``""`` (→ *default*), numbers, and strings with an
    optional ``s``/``m``/``h``/``d`` suffix (e.g. ``"7d"``, ``"12h"``,
    ``"3600"``). Raises :class:`ValueError` on anything unparseable or
    non-positive (zero only when *allow_zero* is set).
    """
    if raw is None:
        return default
    if isinstance(raw, bool):  # bool is an int subclass; reject explicitly
        raise ValueError(f"invalid interval: {raw!r}")
    if isinstance(raw, (int, float)):
        value: float = float(raw)
    elif isinstance(raw, str):
        text = raw.strip().lower()
        if not text:
            return default
        multiplier = 1
        if text[-1] in _SUFFIX_SECONDS and not text[-1].isdigit():
            multiplier = _SUFFIX_SECONDS[text[-1]]
            text = text[:-1].strip()
        try:
            value = float(text) * multiplier
        except ValueError:
            raise ValueError(f"invalid interval: {raw!r}") from None
    else:
        raise ValueError(f"invalid interval: {raw!r}")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"invalid interval (must be positive): {raw!r}")
    return int(value)


def default_refresh() -> list[dict]:
    """Call the crawler's real refresh entrypoint in-process.

    Wires to ``services.crawler.jobs.refresh_stale()`` — the same
    function the RQ worker runs — which opens the catalog, finds stale
    versions, and enqueues one crawl job per version. Imported lazily so
    importing this module stays stdlib-only.
    """
    from services.crawler.jobs import refresh_stale

    return refresh_stale()


def default_devdocs_sync() -> list[dict]:
    """Enqueue the monthly DevDocs image sync (§5.1.1).

    Wires to ``services.crawler.jobs.enqueue_devdocs_sync()``, which puts
    one ``devdocs_sync`` RQ job on the ``sage-crawl`` queue; the
    crawler-worker runs the pull + diff + ingest. The scheduler never
    touches docker or the image itself. Imported lazily so importing this
    module stays stdlib-only.
    """
    from services.crawler.jobs import enqueue_devdocs_sync

    job_id = enqueue_devdocs_sync()
    return [{"job": "devdocs_sync", "rq_job_id": job_id}]


#: Job name → zero-arg callable returning the enqueued-jobs list.
JOB_REFRESHERS: dict[str, Callable[[], list[dict]]] = {
    "refresh_stale": default_refresh,
    "devdocs_sync": default_devdocs_sync,
}

#: Job the scheduler runs unless SCHEDULER_JOB says otherwise.
DEFAULT_JOB = "refresh_stale"


@dataclass
class SchedulerConfig:
    """Resolved scheduler settings."""

    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    startup_jitter_seconds: int = DEFAULT_STARTUP_JITTER_SECONDS
    backoff_cap_multiplier: int = DEFAULT_BACKOFF_CAP_MULTIPLIER
    job: str = DEFAULT_JOB

    @classmethod
    def from_env(cls, env: Any = os.environ) -> "SchedulerConfig":
        """Build config from env; invalid values raise ValueError."""
        get = env.get if hasattr(env, "get") else (lambda k, d=None: env[k] if k in env else d)
        raw_job = get("SCHEDULER_JOB")
        job = DEFAULT_JOB if raw_job is None else str(raw_job).strip()
        if job not in JOB_REFRESHERS:
            raise ValueError(
                "invalid SCHEDULER_JOB: %r (expected one of %s)"
                % (raw_job, sorted(JOB_REFRESHERS))
            )
        return cls(
            interval_seconds=parse_interval_seconds(
                get("REFRESH_INTERVAL_SECONDS"), default=DEFAULT_INTERVAL_SECONDS
            ),
            startup_jitter_seconds=parse_interval_seconds(
                get("SCHEDULER_STARTUP_JITTER_SECONDS"),
                default=DEFAULT_STARTUP_JITTER_SECONDS,
                allow_zero=True,
            ),
            backoff_cap_multiplier=parse_interval_seconds(
                get("SCHEDULER_BACKOFF_CAP_MULTIPLIER"),
                default=DEFAULT_BACKOFF_CAP_MULTIPLIER,
            ),
            job=job,
        )


@dataclass
class RunResult:
    run_id: str
    ok: bool
    enqueued: int = 0
    error: str | None = None
    elapsed_s: float = 0.0


class Scheduler:
    """Runs the configured job on a fixed interval with failure backoff.

    Parameters
    ----------
    refresh: zero-arg callable returning the enqueued-jobs list
        (defaults to the :data:`JOB_REFRESHERS` entry for
        ``config.job``). Tests inject a mock.
    config: :class:`SchedulerConfig` (defaults to :meth:`from_env`).
    clock: object with ``time()`` (injectable for tests).
    sleeper: ``sleep(seconds)`` callable (injectable for tests).
    jitter: ``uniform(lo, hi)`` callable for the startup delay.
    """

    def __init__(
        self,
        *,
        refresh: Callable[[], list[dict]] | None = None,
        config: SchedulerConfig | None = None,
        clock: Any = time,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.config = config or SchedulerConfig.from_env()
        self.refresh = refresh or JOB_REFRESHERS[self.config.job]
        self._clock = clock
        self._sleep = sleeper
        self._jitter = jitter
        self.consecutive_failures = 0
        self.runs = 0

    # -- single sweep --------------------------------------------------------

    def run_once(self) -> RunResult:
        """Execute one refresh sweep. Never raises: failures are logged
        and returned in the result."""
        self.runs += 1
        run_id = f"run-{self.runs:04d}-{int(self._clock.time())}"
        log.info("refresh started run_id=%s", run_id)
        started = self._clock.time()
        try:
            enqueued = self.refresh()
        except Exception as exc:  # never crash the loop
            self.consecutive_failures += 1
            elapsed = self._clock.time() - started
            log.error(
                "refresh failed run_id=%s error=%r failures=%d elapsed=%.1fs",
                run_id,
                exc,
                self.consecutive_failures,
                elapsed,
            )
            return RunResult(
                run_id=run_id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_s=elapsed,
            )
        self.consecutive_failures = 0
        count = len(enqueued) if enqueued is not None else 0
        elapsed = self._clock.time() - started
        log.info(
            "refresh finished run_id=%s enqueued=%d elapsed=%.1fs",
            run_id,
            count,
            elapsed,
        )
        return RunResult(run_id=run_id, ok=True, enqueued=count, elapsed_s=elapsed)

    # -- loop -----------------------------------------------------------------

    def next_interval(self) -> float:
        """Wait before the next run: the base interval on success,
        exponential backoff (capped) after failures."""
        base = float(self.config.interval_seconds)
        if self.consecutive_failures == 0:
            return base
        backoff = base * (2 ** min(self.consecutive_failures, 4))
        cap = base * self.config.backoff_cap_multiplier
        return min(backoff, cap)

    def run_forever(self, *, stop_after: int | None = None) -> None:
        """Loop forever (or *stop_after* runs — a test seam).

        Applies the startup jitter once before the first run. A failing
        sweep logs, backs off, and the loop continues.
        """
        if self.config.startup_jitter_seconds > 0:
            delay = self._jitter(0, self.config.startup_jitter_seconds)
            log.info("scheduler starting: jitter %.1fs before first run", delay)
            self._sleep(delay)
        else:
            log.info("scheduler starting: no startup jitter")
        while True:
            result = self.run_once()
            if stop_after is not None and self.runs >= stop_after:
                return
            wait = self.next_interval()
            log.info(
                "next refresh in %.0fs run_id=%s ok=%s failures=%d",
                wait,
                result.run_id,
                result.ok,
                self.consecutive_failures,
            )
            self._sleep(wait)
