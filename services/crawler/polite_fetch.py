"""Polite HTTP fetching (spec §6).

- robots.txt is obeyed, fail closed on disallow.
- 1 request/second per host (in-memory last-fetch timestamps).
- Exponential backoff on 429/5xx (max 4 retries).
- Identifiable User-Agent.

``PoliteFetcher.fetch(url)`` returns ``(html, final_url)`` following
redirects. The HTTP layer is injectable (``http_get``) so tests never
touch the network; the clock is injectable (``sleep``, ``monotonic``)
so throttle/backoff tests run instantly.
"""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

log = logging.getLogger("sage.crawler.fetch")

USER_AGENT = "NewSageBot/1.0 (+personal research crawler; contact: local)"

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
ROBOTS_TIMEOUT = 10


class RobotsDisallowed(Exception):
    """robots.txt disallows this URL: fail closed (spec §6)."""


class FetchError(Exception):
    """The fetch failed after all retries, or the host refused us."""


def _default_http_get(url, *, headers, timeout, allow_redirects=True):
    import requests

    return requests.get(
        url, headers=headers, timeout=timeout, allow_redirects=allow_redirects
    )


class PoliteFetcher:
    """One fetcher instance = one shared per-host throttle table.

    A single instance should be shared across a crawl so the 1 req/s
    budget is actually enforced; do not construct one per URL.
    """

    def __init__(
        self,
        *,
        http_get=None,
        sleep=None,
        monotonic=None,
        min_interval: float = 1.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        user_agent: str = USER_AGENT,
        timeout: int = 30,
    ) -> None:
        self._http_get = http_get or _default_http_get
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.user_agent = user_agent
        self.timeout = timeout
        self._lock = threading.Lock()
        self._last_fetch: dict[str, float] = {}  # host -> monotonic ts
        self._robots_cache: dict[str, RobotFileParser | None] = {}

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------
    def fetch(self, url: str) -> tuple[str, str]:
        """Fetch ``url`` politely. Returns ``(html, final_url)``.

        Raises :class:`RobotsDisallowed` when robots.txt disallows the
        URL, :class:`FetchError` when the host keeps failing.
        """
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise FetchError(f"refusing to fetch non-HTTP(S) URL: {url!r}")
        if not self._robots_allows(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        headers = {"User-Agent": self.user_agent}
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle(parsed.netloc)
            try:
                resp = self._http_get(
                    url, headers=headers, timeout=self.timeout,
                    allow_redirects=True,
                )
            except Exception as exc:  # network-level failure: retryable
                last_error = exc
                log.warning("fetch %s failed (attempt %d): %s", url, attempt, exc)
            else:
                status = resp.status_code
                if status == 200:
                    return resp.text, resp.url
                if status in RETRYABLE_STATUSES and attempt < self.max_retries:
                    wait = self._retry_wait(resp, attempt)
                    log.warning(
                        "fetch %s -> %s, backing off %.1fs (attempt %d)",
                        url, status, wait, attempt,
                    )
                    self._sleep(wait)
                    continue
                raise FetchError(f"fetch {url} -> HTTP {status}")
            if attempt < self.max_retries:
                wait = min(self.backoff_base * (2**attempt), self.backoff_cap)
                self._sleep(wait)
        raise FetchError(f"fetch {url} failed after {self.max_retries + 1} attempts: {last_error}")

    # ------------------------------------------------------------------
    # throttling
    # ------------------------------------------------------------------
    def _throttle(self, host: str) -> None:
        host = host.lower()
        with self._lock:
            last = self._last_fetch.get(host)
            now = self._monotonic()
            wait = 0.0
            if last is not None:
                wait = self.min_interval - (now - last)
            if wait > 0:
                sleep_for = wait
            else:
                self._last_fetch[host] = now
                return
        # Sleep outside the lock; re-stamp after waking so concurrent
        # threads serialize on the fresh timestamp.
        self._sleep(sleep_for)
        with self._lock:
            self._last_fetch[host] = self._monotonic()

    # ------------------------------------------------------------------
    # robots
    # ------------------------------------------------------------------
    def _robots_allows(self, url: str) -> bool:
        parsed = urlsplit(url)
        host = parsed.netloc.lower()
        with self._lock:
            cached = self._robots_cache.get(host, "missing")
        if cached == "missing":
            cached = self._load_robots(parsed)
            with self._lock:
                self._robots_cache[host] = cached
        if cached is None:
            return True  # no usable robots.txt: allow (fail open on *fetch* error)
        return cached.can_fetch(self.user_agent, url)

    def _load_robots(self, parsed) -> RobotFileParser | None:
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = RobotFileParser()
        rp.set_url(robots_url)
        try:
            # Throttled like any other request to this host; no robots
            # check here (that would recurse).
            self._throttle(parsed.netloc)
            resp = self._http_get(
                robots_url,
                headers={"User-Agent": self.user_agent},
                timeout=ROBOTS_TIMEOUT,
                allow_redirects=True,
            )
        except Exception as exc:
            log.warning("robots.txt fetch failed for %s: %s", robots_url, exc)
            return None
        if resp.status_code != 200:
            return None  # absent robots.txt: everything allowed
        try:
            rp.parse(resp.text.splitlines())
        except Exception as exc:
            log.warning("robots.txt parse failed for %s: %s", robots_url, exc)
            return None
        return rp

    # ------------------------------------------------------------------
    # backoff
    # ------------------------------------------------------------------
    def _retry_wait(self, resp, attempt: int) -> float:
        retry_after = None
        headers = getattr(resp, "headers", {}) or {}
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw is not None:
            try:
                retry_after = float(str(raw).strip())
            except ValueError:
                retry_after = None
        backoff = min(self.backoff_base * (2**attempt), self.backoff_cap)
        if retry_after is not None:
            return min(max(retry_after, 0.0), 60.0)
        return backoff
