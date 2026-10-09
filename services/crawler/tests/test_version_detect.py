"""Tests for version_detect (all offline: injected fetch/run)."""

from types import SimpleNamespace

import pytest

from services.crawler.polite_fetch import FetchError
from services.crawler.version_detect import (
    DetectionResult,
    detect,
    docusaurus_versions,
    git_tags,
    latest_redirect,
    sphinx_versions_json,
)


def fetch_of(routes):
    def fetch(url):
        if url not in routes:
            raise FetchError(f"no route: {url}")
        return routes[url], url

    return fetch


# ---------------------------------------------------------------- sphinx ---


def test_sphinx_versions_json_maps_versions():
    fetch = fetch_of(
        {
            "https://docs.example.com/versions.json": (
                '[{"version": "3.1", "url": "/3.1/"},'
                ' {"version": "3.0", "url": "https://docs.example.com/3.0/"}]'
            )
        }
    )
    result = sphinx_versions_json("https://docs.example.com", fetch=fetch)
    assert result == {
        "3.1": "https://docs.example.com/3.1/",
        "3.0": "https://docs.example.com/3.0/",
    }


def test_sphinx_versions_json_none_when_missing():
    fetch = fetch_of({})
    assert sphinx_versions_json("https://docs.example.com", fetch=fetch) is None


def test_sphinx_versions_json_none_when_not_a_list():
    fetch = fetch_of({"https://docs.example.com/versions.json": '{"a": 1}'})
    assert sphinx_versions_json("https://docs.example.com", fetch=fetch) is None


# ------------------------------------------------------------- docusaurus ---


def test_docusaurus_versions_json_list():
    fetch = fetch_of(
        {"https://docs.example.com/versions.json": '["current", "2.1", "2.0"]'}
    )
    result = docusaurus_versions("https://docs.example.com", fetch=fetch)
    assert result == {
        "current": "https://docs.example.com/docs/",
        "2.1": "https://docs.example.com/docs/2.1/",
        "2.0": "https://docs.example.com/docs/2.0/",
    }


def test_docusaurus_dropdown_fallback():
    html = """
    <html><body>
      <select>
        <option value="/docs/1.9/">1.9</option>
        <option value="/docs/1.8/">1.8</option>
      </select>
      <a href="/docs/next/">next</a>
    </body></html>
    """
    fetch = fetch_of(
        {
            "https://docs.example.com/versions.json": "not json",
            "https://docs.example.com/": html,
        }
    )
    result = docusaurus_versions("https://docs.example.com", fetch=fetch)
    assert result == {
        "1.9": "https://docs.example.com/docs/1.9/",
        "1.8": "https://docs.example.com/docs/1.8/",
        "next": "https://docs.example.com/docs/next/",
    }


def test_docusaurus_none_when_nothing_found():
    fetch = fetch_of({"https://docs.example.com/": "<html><body>no versions</body></html>"})
    assert docusaurus_versions("https://docs.example.com", fetch=fetch) is None


# ------------------------------------------------------------------ latest ---


def test_latest_redirect_resolves():
    def fetch(url):
        return "<html/>", "https://docs.example.com/v9/"

    assert (
        latest_redirect("https://docs.example.com", fetch=fetch)
        == "https://docs.example.com/v9/"
    )


def test_latest_redirect_none_when_no_redirect():
    def fetch(url):
        return "<html/>", url  # no movement

    assert latest_redirect("https://docs.example.com", fetch=fetch) is None


def test_latest_redirect_none_when_missing():
    assert latest_redirect("https://docs.example.com", fetch=fetch_of({})) is None


# ---------------------------------------------------------------- git tags ---


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_git_tags_lists_and_dedupes():
    def run(cmd, **kwargs):
        assert cmd[:3] == ["git", "ls-remote", "--tags"]
        return FakeProc(
            0,
            stdout=(
                "a" * 40 + "\trefs/tags/v2.0.0\n"
                + "b" * 40 + "\trefs/tags/v2.0.0^{}\n"
                + "c" * 40 + "\trefs/tags/v1.0.0\n"
            ),
        )

    assert git_tags("https://github.com/acme/widget", run=run) == ["v2.0.0", "v1.0.0"]


def test_git_tags_none_on_failure():
    def run(cmd, **kwargs):
        return FakeProc(128, stderr="not a repo")

    assert git_tags("https://github.com/acme/nope", run=run) is None


def test_git_tags_none_when_no_tags():
    def run(cmd, **kwargs):
        return FakeProc(0, stdout="")

    assert git_tags("https://github.com/acme/empty", run=run) is None


# ------------------------------------------------------------------- detect ---


def lib(docs_url=None, repo_url=None):
    return SimpleNamespace(docs_url=docs_url, repo_url=repo_url)


def test_detect_prefers_sphinx():
    fetch = fetch_of(
        {"https://docs.example.com/versions.json": '[{"version": "3.1", "url": "/3.1/"}]'}
    )
    result = detect(lib(docs_url="https://docs.example.com"), fetch=fetch)
    assert isinstance(result, DetectionResult)
    assert result.strategy == "sphinx_versions_json"
    assert result.versions == {"3.1": "https://docs.example.com/3.1/"}


def test_detect_falls_through_to_docusaurus():
    fetch = fetch_of(
        {
            "https://docs.example.com/versions.json": "garbage",
            "https://docs.example.com/": '<a href="/docs/2.0/">2.0</a>',
        }
    )
    result = detect(lib(docs_url="https://docs.example.com"), fetch=fetch)
    assert result.strategy == "docusaurus_versions"
    assert result.versions == {"2.0": "https://docs.example.com/docs/2.0/"}


def test_detect_falls_through_to_latest_redirect():
    def fetch(url):
        if url.endswith("/latest/"):
            return "<html/>", "https://docs.example.com/v9/"
        raise FetchError(url)

    result = detect(lib(docs_url="https://docs.example.com"), fetch=fetch)
    assert result.strategy == "latest_redirect"
    assert result.resolved_url == "https://docs.example.com/v9/"


def test_detect_uses_git_tags_for_repo_only():
    def run(cmd, **kwargs):
        return FakeProc(0, stdout="d" * 40 + "\trefs/tags/1.2.3\n")

    result = detect(lib(repo_url="https://github.com/acme/widget"), run=run)
    assert result.strategy == "git_tags"
    assert result.tags == ["1.2.3"]


def test_detect_none_when_nothing_applies():
    result = detect(lib(), fetch=fetch_of({}))
    assert result is None
