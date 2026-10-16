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

import ipaddress
import logging
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit
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
    def _validate_destination(self, url: str) -> None:
        """Reject local/private destinations before every HTTP request or hop.

        Production resolves DNS for an additional SSRF check. An outbound
        firewall is still required to prevent DNS rebinding and proxy bypass.
        """
        try:
            parsed = urlsplit(url)
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise FetchError("invalid URL destination") from exc
        if (parsed.scheme not in ("http", "https") or not host
                or parsed.username or parsed.password or port == 0):
            raise FetchError(f"refusing unsafe crawl URL: {url!r}")
        if host.lower().rstrip(".") in {"localhost", "metadata.google.internal"} or host.lower().endswith((".local", ".internal", ".localhost")):
            raise FetchError(f"refusing local crawl host: {host}")
        try:
            addr = ipaddress.ip_address(host)
            if not addr.is_global:
                raise FetchError(f"refusing nonpublic crawl address: {host}")
        except ValueError:
            pass
        # Fakes should not perform DNS/network calls; real HTTP must do so.
        if self._http_get is _default_http_get:
            try:
                addresses = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            except OSError as exc:
                raise FetchError(f"crawl DNS lookup failed for {host}: {exc}") from exc
            if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                raise FetchError(f"crawl destination resolves to nonpublic address: {host}")

    def fetch(self, url: str) -> tuple[str, str]:
        """Fetch with URL validation on each manual redirect hop."""
        self._validate_destination(url)
        headers = {"User-Agent": self.user_agent}
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            current = url
            try:
                for redirects in range(6):
                    self._validate_destination(current)
                    if not self._robots_allows(current):
                        raise RobotsDisallowed(f"robots.txt disallows {current}")
                    self._throttle(urlsplit(current).netloc)
                    resp = self._http_get(
                        current, headers=headers, timeout=self.timeout,
                        allow_redirects=False,
                    )
                    status = resp.status_code
                    if status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location") or resp.headers.get("location")
                        if not location or redirects == 5:
                            raise FetchError("crawl redirects exceeded or lacked Location")
                        current = urljoin(current, location)
                        continue
                    if status == 200:
                        final = resp.url or current
                        self._validate_destination(final)
                        return resp.text, final
                    if status in RETRYABLE_STATUSES and attempt < self.max_retries:
                        wait = self._retry_wait(resp, attempt)
                        log.warning("fetch %s -> %s, backing off %.1fs", current, status, wait)
                        self._sleep(wait)
                        break
                    raise FetchError(f"fetch {current} -> HTTP {status}")
            except (FetchError, RobotsDisallowed):
                raise
            except Exception as exc:
                last_error = exc
                log.warning("fetch %s failed (attempt %d): %s", current, attempt, exc)
                if attempt < self.max_retries:
                    self._sleep(min(self.backoff_base * (2 ** attempt), self.backoff_cap))
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
                allow_redirects=False,
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
