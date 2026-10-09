"""Tests for polite_fetch (all offline: injected http_get + fake clock)."""

import pytest

from services.crawler.polite_fetch import (
    USER_AGENT,
    FetchError,
    PoliteFetcher,
    RobotsDisallowed,
)


class FakeResponse:
    def __init__(self, status_code=200, text="", url="", headers=None):
        self.status_code = status_code
        self.text = text
        self.url = url or ""
        self.headers = headers or {}


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def make_fetcher(routes, clock=None, **kwargs):
    """routes: url -> FakeResponse | Exception."""
    clock = clock or Clock()
    calls: list[str] = []

    def http_get(url, *, headers, timeout, allow_redirects=True):
        calls.append(url)
        assert headers.get("User-Agent") == USER_AGENT
        resp = routes.get(url)
        if resp is None:
            raise FetchError(f"no route for {url}")
        if isinstance(resp, Exception):
            raise resp
        if not resp.url:
            resp.url = url
        return resp

    fetcher = PoliteFetcher(
        http_get=http_get,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        **kwargs,
    )
    return fetcher, calls, clock


ROBOTS_ALLOW = FakeResponse(200, "User-agent: *\nDisallow:\n")
ROBOTS_DENY = FakeResponse(200, "User-agent: *\nDisallow: /private\n")


def test_robots_disallow_fails_closed():
    fetcher, calls, _ = make_fetcher(
        {"http://h.test/robots.txt": ROBOTS_DENY}
    )
    with pytest.raises(RobotsDisallowed):
        fetcher.fetch("http://h.test/private/page")
    # The page itself was never requested.
    assert calls == ["http://h.test/robots.txt"]


def test_robots_allow_proceeds():
    page = FakeResponse(200, "<html>hi</html>")
    fetcher, calls, _ = make_fetcher(
        {
            "http://h.test/robots.txt": ROBOTS_ALLOW,
            "http://h.test/page": page,
        }
    )
    html, final = fetcher.fetch("http://h.test/page")
    assert html == "<html>hi</html>"
    assert final == "http://h.test/page"
    assert "http://h.test/page" in calls


def test_robots_fetch_error_fails_open():
    # robots.txt unreachable: allowed (documented), page still fetched.
    page = FakeResponse(200, "ok")
    fetcher, _, _ = make_fetcher(
        {
            "http://h.test/robots.txt": ConnectionError("dns"),
            "http://h.test/page": page,
        }
    )
    html, _ = fetcher.fetch("http://h.test/page")
    assert html == "ok"


def test_throttle_one_req_per_second_per_host():
    clock = Clock()
    page = FakeResponse(200, "ok")
    fetcher, _, _ = make_fetcher(
        {
            "http://h.test/robots.txt": ROBOTS_ALLOW,
            "http://h.test/a": page,
            "http://h.test/b": page,
        },
        clock,
        min_interval=1.0,
    )
    fetcher.fetch("http://h.test/a")  # robots (no wait) + page (waits 1.0)
    fetcher.fetch("http://h.test/b")  # robots cached; page waits 1.0
    assert clock.sleeps == pytest.approx([1.0, 1.0])


def test_throttle_is_per_host():
    clock = Clock()
    page = FakeResponse(200, "ok")
    fetcher, _, _ = make_fetcher(
        {
            "http://a.test/robots.txt": ROBOTS_ALLOW,
            "http://a.test/x": page,
            "http://b.test/robots.txt": ROBOTS_ALLOW,
            "http://b.test/x": page,
        },
        clock,
        min_interval=1.0,
    )
    fetcher.fetch("http://a.test/x")
    fetcher.fetch("http://b.test/x")  # different host: only its own robots+page wait
    assert clock.sleeps == pytest.approx([1.0, 1.0])


def test_retry_on_429_with_retry_after():
    clock = Clock()
    url = "http://h.test/page"
    responses = iter(
        [
            FakeResponse(429, "slow", headers={"Retry-After": "2"}),
            FakeResponse(200, "finally", url=url),
        ]
    )

    def http_get(u, *, headers, timeout, allow_redirects=True):
        if u.endswith("/robots.txt"):
            return ROBOTS_ALLOW
        return next(responses)

    fetcher = PoliteFetcher(
        http_get=http_get, sleep=clock.sleep, monotonic=clock.monotonic,
        min_interval=0,
    )
    html, _ = fetcher.fetch(url)
    assert html == "finally"
    assert clock.sleeps == pytest.approx([2.0])  # Retry-After honored


def test_retry_on_500_exponential_backoff():
    clock = Clock()
    url = "http://h.test/page"
    responses = iter(
        [
            FakeResponse(500, "err"),
            FakeResponse(503, "err"),
            FakeResponse(200, "ok", url=url),
        ]
    )

    def http_get(u, *, headers, timeout, allow_redirects=True):
        if u.endswith("/robots.txt"):
            return ROBOTS_ALLOW
        return next(responses)

    fetcher = PoliteFetcher(
        http_get=http_get, sleep=clock.sleep, monotonic=clock.monotonic,
        backoff_base=1.0, min_interval=0,
    )
    html, _ = fetcher.fetch(url)
    assert html == "ok"
    assert clock.sleeps == pytest.approx([1.0, 2.0])


def test_gives_up_after_max_retries():
    clock = Clock()
    calls = []

    def http_get(u, *, headers, timeout, allow_redirects=True):
        if u.endswith("/robots.txt"):
            return ROBOTS_ALLOW
        calls.append(u)
        return FakeResponse(503, "down")

    fetcher = PoliteFetcher(
        http_get=http_get, sleep=clock.sleep, monotonic=clock.monotonic,
        max_retries=2, backoff_base=1.0, min_interval=0,
    )
    with pytest.raises(FetchError):
        fetcher.fetch("http://h.test/page")
    assert len(calls) == 3  # initial + 2 retries
    assert clock.sleeps == pytest.approx([1.0, 2.0])


def test_404_is_not_retried():
    def http_get(u, *, headers, timeout, allow_redirects=True):
        if u.endswith("/robots.txt"):
            return ROBOTS_ALLOW
        return FakeResponse(404, "nope")

    fetcher = PoliteFetcher(http_get=http_get, sleep=lambda s: None)
    with pytest.raises(FetchError):
        fetcher.fetch("http://h.test/missing")


def test_redirects_followed_final_url_returned():
    def http_get(u, *, headers, timeout, allow_redirects=True):
        if u.endswith("/robots.txt"):
            return ROBOTS_ALLOW
        return FakeResponse(200, "new home", url="http://h.test/new-home")

    fetcher = PoliteFetcher(http_get=http_get, sleep=lambda s: None)
    html, final = fetcher.fetch("http://h.test/old-home")
    assert (html, final) == ("new home", "http://h.test/new-home")


def test_non_http_url_refused():
    fetcher, _, _ = make_fetcher({})
    with pytest.raises(FetchError):
        fetcher.fetch("ftp://h.test/file")
