"""Scheduler CLI.

Run one refresh sweep immediately and exit::

    python -m scheduler.run --once
    python -m services.scheduler.run --once     # equivalent

Or run the daemon loop (compose default)::

    python -m scheduler.run

Exit code for ``--once``: 0 on a successful sweep, 1 on failure,
2 on bad configuration/arguments.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# Import the scheduler package. Both of these must work:
#   python -m scheduler.run            (services/ on sys.path)
#   python -m services.scheduler.run   (build root on sys.path)
# When neither is on the path (ad-hoc runs), fall back to resolving them
# relative to this file. The fallback adds the *parent* of the package,
# never the package dir itself, so `scheduler` always resolves to the
# package rather than being shadowed by scheduler.py.
_HERE = os.path.dirname(os.path.abspath(__file__))  # services/scheduler
_SERVICES_DIR = os.path.dirname(_HERE)  # services/
_BUILD_ROOT = os.path.dirname(_SERVICES_DIR)  # build root

try:
    from scheduler.scheduler import (  # noqa: E402
        DEFAULT_INTERVAL_SECONDS,
        Scheduler,
        SchedulerConfig,
        parse_interval_seconds,
    )
except ImportError:
    for _p in (_SERVICES_DIR, _BUILD_ROOT):
        if _p not in sys.path:
            sys.path.insert(0, _p)
    from scheduler.scheduler import (  # noqa: E402
        DEFAULT_INTERVAL_SECONDS,
        Scheduler,
        SchedulerConfig,
        parse_interval_seconds,
    )

# default_refresh() lazily imports services.crawler.jobs; make sure the
# build root is importable for that regardless of invocation style.
if _BUILD_ROOT not in sys.path:
    sys.path.append(_BUILD_ROOT)

log = logging.getLogger("sage.scheduler.cli")


def build_config(args: argparse.Namespace, env: object = os.environ) -> SchedulerConfig:
    """Config from env, with the ``--interval-seconds`` CLI override winning.

    Raises SystemExit(2) with a clear message on invalid values.
    """
    try:
        config = SchedulerConfig.from_env(env)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    if args.interval_seconds is not None:
        try:
            config.interval_seconds = parse_interval_seconds(
                args.interval_seconds, default=DEFAULT_INTERVAL_SECONDS
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(2) from None
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scheduler.run",
        description="New_sage weekly refresh scheduler: runs the crawler's "
        "refresh_stale() sweep on a fixed interval.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single sweep now and exit (0 ok / 1 sweep failed)",
    )
    parser.add_argument(
        "--interval-seconds",
        default=None,
        help="override REFRESH_INTERVAL_SECONDS (seconds or 7d/12h/30m suffix)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("SCHEDULER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    config = build_config(args)

    scheduler = Scheduler(config=config)
    if args.once:
        result = scheduler.run_once()
        print(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "ok": result.ok,
                    "enqueued": result.enqueued,
                    "error": result.error,
                    "elapsed_s": round(result.elapsed_s, 1),
                }
            )
        )
        return 0 if result.ok else 1

    log.info(
        "scheduler daemon: interval=%ds jitter<=%ds (SCHEDULER_STARTUP_JITTER_SECONDS)",
        config.interval_seconds,
        config.startup_jitter_seconds,
    )
    scheduler.run_forever()
    return 0  # unreachable; run_forever only returns via stop_after (tests)


if __name__ == "__main__":
    sys.exit(main())
