"""Tests for the New_sage refresh scheduler (stdlib only; the refresh call
is always mocked — no Postgres/Redis/crawler imports at test time)."""

import pytest

from scheduler.scheduler import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_JOB,
    DEVDOCS_SYNC_INTERVAL_SECONDS,
    JOB_REFRESHERS,
    Scheduler,
    SchedulerConfig,
    default_devdocs_sync,
    default_refresh,
    parse_interval_seconds,
)


# ---------------------------------------------------------------------------
# interval parsing
# ---------------------------------------------------------------------------


class TestParseIntervalSeconds:
    def test_none_and_empty_give_default(self):
        assert parse_interval_seconds(None) == DEFAULT_INTERVAL_SECONDS
        assert parse_interval_seconds("") == DEFAULT_INTERVAL_SECONDS
        assert parse_interval_seconds("   ") == DEFAULT_INTERVAL_SECONDS

    def test_plain_numbers(self):
        assert parse_interval_seconds("604800") == 604800
        assert parse_interval_seconds("3600.9") == 3600  # truncated
        assert parse_interval_seconds(60) == 60
        assert parse_interval_seconds(90.0) == 90

    def test_suffixes(self):
        assert parse_interval_seconds("7d") == 7 * 86400
        assert parse_interval_seconds("12h") == 12 * 3600
        assert parse_interval_seconds("30m") == 30 * 60
        assert parse_interval_seconds("45s") == 45
        assert parse_interval_seconds(" 7D ") == 7 * 86400  # case/space tolerant
        assert parse_interval_seconds("1.5h") == 5400

    @pytest.mark.parametrize("raw", ["abc", "7x", "-", "1.2.3", "--5"])
    def test_garbage_raises(self, raw):
        with pytest.raises(ValueError):
            parse_interval_seconds(raw)

    @pytest.mark.parametrize("raw", ["0", "-60", "-1d", 0, -5])
    def test_non_positive_raises(self, raw):
        with pytest.raises(ValueError):
            parse_interval_seconds(raw)

    def test_zero_allowed_with_flag(self):
        assert parse_interval_seconds("0", allow_zero=True) == 0
        assert parse_interval_seconds(0, allow_zero=True) == 0

    def test_bool_rejected(self):
        with pytest.raises(ValueError):
            parse_interval_seconds(True)


class TestConfigFromEnv:
    def test_defaults(self):
        cfg = SchedulerConfig.from_env({})
        assert cfg.interval_seconds == DEFAULT_INTERVAL_SECONDS
        assert cfg.startup_jitter_seconds == 600
        assert cfg.backoff_cap_multiplier == 8

    def test_env_overrides(self):
        cfg = SchedulerConfig.from_env(
            {
                "REFRESH_INTERVAL_SECONDS": "1d",
                "SCHEDULER_STARTUP_JITTER_SECONDS": "0",
                "SCHEDULER_BACKOFF_CAP_MULTIPLIER": "4",
            }
        )
        assert cfg.interval_seconds == 86400
        assert cfg.startup_jitter_seconds == 0
        assert cfg.backoff_cap_multiplier == 4

    def test_invalid_env_raises(self):
        with pytest.raises(ValueError):
            SchedulerConfig.from_env({"REFRESH_INTERVAL_SECONDS": "soon"})


# ---------------------------------------------------------------------------
# test doubles
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSleeper:
    def __init__(self, clock):
        self.clock = clock
        self.sleeps = []

    def __call__(self, seconds):
        self.sleeps.append(seconds)
        self.clock.advance(seconds)


def make_scheduler(refresh=None, **cfg_kwargs):
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    fields = {
        "interval_seconds": 3600,
        "startup_jitter_seconds": 0,
        "backoff_cap_multiplier": 8,
    }
    fields.update(cfg_kwargs)
    sched = Scheduler(refresh=refresh, config=SchedulerConfig(**fields), clock=clock, sleeper=sleeper)
    return sched, clock, sleeper


# ---------------------------------------------------------------------------
# run_once: success, failure, structured logging
# ---------------------------------------------------------------------------


class TestRunOnce:
    def test_success_returns_enqueued_count(self):
        sched, _, _ = make_scheduler(refresh=lambda: [{"a": 1}, {"b": 2}])
        result = sched.run_once()
        assert result.ok is True
        assert result.enqueued == 2
        assert result.error is None
        assert sched.consecutive_failures == 0
        assert sched.runs == 1

    def test_success_with_none_result_counts_zero(self):
        sched, _, _ = make_scheduler(refresh=lambda: None)
        assert sched.run_once().enqueued == 0

    def test_failure_is_captured_not_raised(self):
        def boom():
            raise RuntimeError("db exploded")

        sched, _, _ = make_scheduler(refresh=boom)
        result = sched.run_once()
        assert result.ok is False
        assert result.enqueued == 0
        assert "RuntimeError" in result.error
        assert "db exploded" in result.error
        assert sched.consecutive_failures == 1

    def test_success_resets_failure_counter(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("once")
            return []

        sched, _, _ = make_scheduler(refresh=flaky)
        assert sched.run_once().ok is False
        assert sched.consecutive_failures == 1
        assert sched.run_once().ok is True
        assert sched.consecutive_failures == 0

    def test_run_id_increments(self, caplog):
        import logging

        sched, _, _ = make_scheduler(refresh=lambda: [])
        with caplog.at_level(logging.INFO, logger="sage.scheduler"):
            sched.run_once()
            sched.run_once()
        starts = [r for r in caplog.records if "refresh started" in r.message]
        assert len(starts) == 2
        assert starts[0].message != starts[1].message  # distinct run ids

    def test_structured_log_fields(self, caplog):
        import logging

        sched, _, _ = make_scheduler(refresh=lambda: [{"x": 1}])
        with caplog.at_level(logging.INFO, logger="sage.scheduler"):
            sched.run_once()
        finished = [r for r in caplog.records if "refresh finished" in r.message]
        assert finished, "expected a 'refresh finished' log line"
        assert "enqueued=1" in finished[0].message
        assert "run_id=" in finished[0].message

        def boom():
            raise ValueError("nope")

        sched2, _, _ = make_scheduler(refresh=boom)
        with caplog.at_level(logging.ERROR, logger="sage.scheduler"):
            sched2.run_once()
        failed = [r for r in caplog.records if "refresh failed" in r.message]
        assert failed, "expected a 'refresh failed' log line"
        assert "failures=1" in failed[0].message


# ---------------------------------------------------------------------------
# backoff
# ---------------------------------------------------------------------------


class TestBackoff:
    def test_success_uses_base_interval(self):
        sched, _, _ = make_scheduler(refresh=lambda: [])
        assert sched.next_interval() == 3600

    def test_failures_double_the_wait(self):
        sched, _, _ = make_scheduler(refresh=lambda: [])
        sched.consecutive_failures = 1
        assert sched.next_interval() == 7200
        sched.consecutive_failures = 2
        assert sched.next_interval() == 14400

    def test_backoff_is_capped(self):
        sched, _, _ = make_scheduler(refresh=lambda: [])
        sched.consecutive_failures = 100
        assert sched.next_interval() == 3600 * 8  # 2**4 growth, capped at 8x

    def test_run_forever_backs_off_and_never_raises(self):
        sched, _, sleeper = make_scheduler(
            refresh=lambda: (_ for _ in ()).throw(RuntimeError("always"))
        )
        sched.run_forever(stop_after=3)  # would raise if failures propagated
        assert sched.runs == 3
        assert sched.consecutive_failures == 3
        # sleeps: after run 1 → 2×interval, after run 2 → 4×interval
        assert sleeper.sleeps == [7200.0, 14400.0]

    def test_run_forever_sleeps_base_interval_on_success(self):
        sched, _, sleeper = make_scheduler(refresh=lambda: [])
        sched.run_forever(stop_after=2)
        assert sleeper.sleeps == [3600.0]


# ---------------------------------------------------------------------------
# startup jitter
# ---------------------------------------------------------------------------


class TestStartupJitter:
    def test_no_jitter_when_configured_zero(self):
        sched, _, sleeper = make_scheduler(
            refresh=lambda: [], startup_jitter_seconds=0
        )
        sched.run_forever(stop_after=1)
        assert sleeper.sleeps == []  # only jitter would have slept

    def test_jitter_sleeps_before_first_run(self):
        clock = FakeClock()
        sleeper = FakeSleeper(clock)
        cfg = SchedulerConfig(
            interval_seconds=3600, startup_jitter_seconds=600, backoff_cap_multiplier=8
        )
        sched = Scheduler(
            refresh=lambda: [],
            config=cfg,
            clock=clock,
            sleeper=sleeper,
            jitter=lambda lo, hi: 123.0,
        )
        sched.run_forever(stop_after=1)
        assert sleeper.sleeps[0] == 123.0
        assert sched.runs == 1


# ---------------------------------------------------------------------------
# CLI: --once mode
# ---------------------------------------------------------------------------


class TestCliOnce:
    def _run_main(self, monkeypatch, argv, refresh):
        from scheduler import run as run_module  # `python -m scheduler.run`

        calls = {"n": 0}

        def fake_refresh():
            calls["n"] += 1
            return refresh()

        class FakeScheduler:
            def __init__(self, config):
                self._sched = Scheduler(
                    refresh=fake_refresh,
                    config=config,
                    clock=FakeClock(),
                    sleeper=FakeSleeper(FakeClock()),
                )

            def run_once(self):
                return self._sched.run_once()

        monkeypatch.setattr(run_module, "Scheduler", FakeScheduler)
        return run_module.main(argv), calls["n"]

    def test_once_success_exit_zero(self, monkeypatch, capsys):
        rc, calls = self._run_main(monkeypatch, ["--once"], lambda: [{"a": 1}])
        assert rc == 0
        assert calls == 1
        import json

        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True and out["enqueued"] == 1

    def test_once_failure_exit_one(self, monkeypatch, capsys):
        def boom():
            raise RuntimeError("db down")

        rc, calls = self._run_main(monkeypatch, ["--once"], boom)
        assert rc == 1
        assert calls == 1
        import json

        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False
        assert "db down" in out["error"]

    def test_bad_interval_exit_two(self, monkeypatch, capsys):
        from scheduler import run as run_module  # `python -m scheduler.run`

        monkeypatch.setattr(run_module, "Scheduler", lambda config: None)
        with pytest.raises(SystemExit) as exc:
            run_module.main(["--once", "--interval-seconds", "soon"])
        assert exc.value.code == 2

    def test_interval_override_wins(self, monkeypatch):
        from scheduler import run as run_module  # `python -m scheduler.run`

        seen = {}

        class FakeScheduler:
            def __init__(self, config):
                seen["interval"] = config.interval_seconds

            def run_once(self):
                from scheduler.scheduler import RunResult

                return RunResult(run_id="x", ok=True)

        monkeypatch.setattr(run_module, "Scheduler", FakeScheduler)
        rc = run_module.main(["--once", "--interval-seconds", "2d"])
        assert rc == 0
        assert seen["interval"] == 2 * 86400


# ---------------------------------------------------------------------------
# job selection: refresh_stale (weekly) vs devdocs_sync (monthly)
# ---------------------------------------------------------------------------


class TestJobSelection:
    def test_default_job_is_refresh_stale(self):
        cfg = SchedulerConfig.from_env({})
        assert cfg.job == "refresh_stale"
        assert cfg.job == DEFAULT_JOB

    def test_job_env_override(self):
        cfg = SchedulerConfig.from_env({"SCHEDULER_JOB": "devdocs_sync"})
        assert cfg.job == "devdocs_sync"

    def test_job_env_whitespace_tolerated(self):
        cfg = SchedulerConfig.from_env({"SCHEDULER_JOB": "  devdocs_sync "})
        assert cfg.job == "devdocs_sync"

    def test_invalid_job_raises(self):
        with pytest.raises(ValueError, match="SCHEDULER_JOB"):
            SchedulerConfig.from_env({"SCHEDULER_JOB": "nope"})

    def test_job_registry(self):
        assert JOB_REFRESHERS["refresh_stale"] is default_refresh
        assert JOB_REFRESHERS["devdocs_sync"] is default_devdocs_sync

    def test_monthly_cadence_registered(self):
        """The monthly DevDocs sync cadence (§5.1.1, §17.6): 30 days."""
        assert DEVDOCS_SYNC_INTERVAL_SECONDS == 30 * 24 * 3600

    def test_scheduler_defaults_refresh_from_job(self):
        sched, _, _ = make_scheduler(refresh=None, job="devdocs_sync")
        assert sched.refresh is default_devdocs_sync

    def test_scheduler_default_is_refresh_stale(self):
        sched, _, _ = make_scheduler(refresh=None)
        assert sched.refresh is default_refresh

    def test_explicit_refresh_wins_over_job(self):
        sentinel = lambda: []
        sched, _, _ = make_scheduler(refresh=sentinel, job="devdocs_sync")
        assert sched.refresh is sentinel


class TestDefaultDevdocsSync:
    def test_enqueues_one_sync_job(self, monkeypatch):
        import sys
        import types

        fake = types.ModuleType("services.crawler.jobs")
        calls = []

        def fake_enqueue(**kwargs):
            calls.append(kwargs)
            return "rq-9"

        fake.enqueue_devdocs_sync = fake_enqueue
        monkeypatch.setitem(sys.modules, "services.crawler.jobs", fake)

        result = default_devdocs_sync()
        assert result == [{"job": "devdocs_sync", "rq_job_id": "rq-9"}]
        assert calls == [{}]


class TestCliJobFlag:
    def _run_main(self, monkeypatch, argv):
        from scheduler import run as run_module  # `python -m scheduler.run`

        seen = {}

        class FakeScheduler:
            def __init__(self, config):
                seen["job"] = config.job
                seen["interval"] = config.interval_seconds

            def run_once(self):
                from scheduler.scheduler import RunResult

                return RunResult(run_id="x", ok=True, enqueued=1)

        monkeypatch.setattr(run_module, "Scheduler", FakeScheduler)
        rc = run_module.main(argv)
        return rc, seen

    def test_job_flag_selects_devdocs_sync(self, monkeypatch):
        rc, seen = self._run_main(monkeypatch, ["--once", "--job", "devdocs_sync"])
        assert rc == 0
        assert seen["job"] == "devdocs_sync"

    def test_job_flag_defaults_to_env(self, monkeypatch):
        monkeypatch.setenv("SCHEDULER_JOB", "devdocs_sync")
        rc, seen = self._run_main(monkeypatch, ["--once"])
        assert rc == 0
        assert seen["job"] == "devdocs_sync"

    def test_job_flag_overrides_env(self, monkeypatch):
        monkeypatch.setenv("SCHEDULER_JOB", "devdocs_sync")
        rc, seen = self._run_main(
            monkeypatch, ["--once", "--job", "refresh_stale"]
        )
        assert rc == 0
        assert seen["job"] == "refresh_stale"

    def test_bad_job_exits_two(self, monkeypatch):
        from scheduler import run as run_module  # `python -m scheduler.run`

        monkeypatch.setattr(run_module, "Scheduler", lambda config: None)
        with pytest.raises(SystemExit) as exc:
            run_module.main(["--once", "--job", "nope"])
        assert exc.value.code == 2

    def test_bad_job_env_exits_two(self, monkeypatch):
        from scheduler import run as run_module  # `python -m scheduler.run`

        monkeypatch.setenv("SCHEDULER_JOB", "nope")
        monkeypatch.setattr(run_module, "Scheduler", lambda config: None)
        with pytest.raises(SystemExit) as exc:
            run_module.main(["--once"])
        assert exc.value.code == 2
