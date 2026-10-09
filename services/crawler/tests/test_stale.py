"""Tests for the stale-while-revalidate predicate (no database)."""

import datetime as dt
from types import SimpleNamespace

from services.crawler.stale import needs_refresh


def row(status="ready", crawled_at=None, refresh_after_days=30):
    return SimpleNamespace(
        status=status, crawled_at=crawled_at, refresh_after_days=refresh_after_days
    )


NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)


def test_stale_when_past_horizon():
    r = row(crawled_at=NOW - dt.timedelta(days=31))
    assert needs_refresh(r, now=NOW) is True


def test_fresh_when_within_horizon():
    r = row(crawled_at=NOW - dt.timedelta(days=29))
    assert needs_refresh(r, now=NOW) is False


def test_custom_refresh_after_days():
    r = row(crawled_at=NOW - dt.timedelta(days=8), refresh_after_days=7)
    assert needs_refresh(r, now=NOW) is True
    r = row(crawled_at=NOW - dt.timedelta(days=6), refresh_after_days=7)
    assert needs_refresh(r, now=NOW) is False


def test_non_ready_rows_are_never_stale():
    old = NOW - dt.timedelta(days=365)
    for status in ("pending", "crawling", "failed"):
        assert needs_refresh(row(status=status, crawled_at=old), now=NOW) is False


def test_never_crawled_is_not_stale():
    assert needs_refresh(row(crawled_at=None), now=NOW) is False


def test_naive_datetime_treated_as_utc():
    # SQLite returns naive datetimes; must not raise or misbehave.
    r = row(crawled_at=(NOW - dt.timedelta(days=40)).replace(tzinfo=None))
    assert needs_refresh(r, now=NOW) is True
