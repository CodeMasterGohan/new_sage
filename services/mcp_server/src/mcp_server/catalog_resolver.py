"""Lazily refreshed catalog-backed MCP library resolver.

PostgreSQL is the authoritative source of library records in production.
Reload on a bounded TTL so newly ingested libraries become discoverable
without restarting the MCP process. No seed-table fallback on DB errors.
"""
from __future__ import annotations

import logging
import threading
import time

from .resolver import LibraryResolver

log = logging.getLogger(__name__)


class CatalogRefreshingResolver:
    def __init__(self, database_url: str, ttl_seconds: int = 60) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        self._url = database_url
        self._ttl = max(1, ttl_seconds)
        self._lock = threading.Lock()
        self._resolver: LibraryResolver | None = None
        self._expires_at = 0.0

    def _current(self) -> LibraryResolver:
        if self._resolver is not None and time.monotonic() < self._expires_at:
            return self._resolver
        with self._lock:
            if self._resolver is None or time.monotonic() >= self._expires_at:
                updated = LibraryResolver.from_catalog(self._url)
                self._resolver = updated
                self._expires_at = time.monotonic() + self._ttl
                log.info("refreshed MCP library catalog resolver")
        return self._resolver

    def get(self, library_id: str):
        return self._current().get(library_id)

    def resolve(self, library_name: str, ecosystem: str | None = None):
        return self._current().resolve(library_name, ecosystem=ecosystem)

    def get_by_context7_id(self, library_id: str):
        return self._current().get_by_context7_id(library_id)
