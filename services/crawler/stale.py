"""Staleness policy helpers (spec §6, §12).

``needs_refresh`` is the stale-while-revalidate predicate shared by the
refresh sweep: ``crawled_at + refresh_after_days < now``. It mirrors
``CatalogRepository.list_stale_versions`` exactly (same naive-datetime
normalization), so unit tests can exercise the policy without a database.
"""

from __future__ import annotations

import datetime as dt


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_aware_utc(value: dt.datetime | None) -> dt.datetime | None:
    # SQLite returns naive datetimes for TIMESTAMPTZ columns; Postgres
    # returns aware ones. Treat naive as UTC — same rule as the catalog
    # repository, so the two can never disagree on staleness.
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def needs_refresh(library_version_row, *, now: dt.datetime | None = None) -> bool:
    """True when a version row is past its refresh horizon.

    Only ``status == "ready"`` rows with a non-null ``crawled_at`` can be
    stale: pending/crawling/failed rows are the responsibility of the job
    that owns them, and a version that never finished crawling has no
    horizon to be past.
    """
    now = _as_aware_utc(now) or _utcnow()
    if getattr(library_version_row, "status", None) != "ready":
        return False
    crawled = _as_aware_utc(getattr(library_version_row, "crawled_at", None))
    if crawled is None:
        return False
    horizon_days = getattr(library_version_row, "refresh_after_days", 30) or 30
    return crawled + dt.timedelta(days=horizon_days) < now
