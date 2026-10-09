"""Version detection strategies (spec §6).

Each strategy maps a docs base URL (or repo URL) to versioned docs URLs.
Every strategy returns ``None`` when it does not apply, so
:func:`detect` can try them in order:

1. ``sphinx_versions_json`` — Sphinx ``versions.json`` (version -> URL map).
2. ``docusaurus_versions`` — Docusaurus ``versions.json`` / version
   dropdown (version -> URL map).
3. ``latest_redirect`` — resolve ``<base>/latest/`` to its final URL
   (single resolved URL).
4. ``git_tags`` — ``git ls-remote --tags`` (version list, no clone).

The network layer (``fetch``) and the git runner (``run``) are injectable
so tests run offline. ``fetch`` must behave like
``polite_fetch.PoliteFetcher.fetch`` (returns ``(html, final_url)``);
``run`` must behave like ``subprocess.run`` returning an object with
``.returncode`` and ``.stdout``.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

log = logging.getLogger("sage.crawler.versions")


@dataclass
class DetectionResult:
    """Normalized outcome of :func:`detect`."""

    strategy: str  # which strategy fired
    versions: dict[str, str] | None = None  # version -> docs URL
    resolved_url: str | None = None  # single URL (latest_redirect)
    tags: list[str] | None = field(default_factory=list)  # git_tags


def _join(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


# ---------------------------------------------------------------------------
# 1. Sphinx versions.json
# ---------------------------------------------------------------------------


def sphinx_versions_json(base: str, *, fetch=None) -> dict[str, str] | None:
    """Parse a Sphinx ``versions.json`` into {version: docs URL}.

    The sphinx-versions extension writes a JSON list of
    ``{"version": ..., "url": ...}`` (sometimes ``"name"`` instead of
    ``"version"``). Relative URLs are resolved against ``base``.
    """
    if fetch is None:
        return None
    url = _join(base, "versions.json")
    try:
        text, _ = fetch(url)
    except Exception as exc:
        log.debug("sphinx versions.json unavailable at %s: %s", url, exc)
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    versions: dict[str, str] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        version = entry.get("version") or entry.get("name")
        link = entry.get("url") or entry.get("link")
        if version and link:
            versions[str(version)] = urljoin(base + "/", str(link))
    return versions or None


# ---------------------------------------------------------------------------
# 2. Docusaurus versions
# ---------------------------------------------------------------------------


class _VersionLinkParser(HTMLParser):
    """Collect hrefs that look like versioned docs links."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ("a", "option"):
            return
        for name, value in attrs:
            if name in ("href", "value") and value:
                self.hrefs.append(value)


_VERSION_PATH_RE = re.compile(r"/docs/([^/]+)/?$")


def docusaurus_versions(base: str, *, fetch=None) -> dict[str, str] | None:
    """Parse Docusaurus versioning into {version: docs URL}.

    Tries ``<base>/versions.json`` (Docusaurus >= 2 writes a JSON list of
    version names; versioned docs live at ``/docs/<version>/``), then
    falls back to scanning the landing page for a version dropdown /
    versioned links.
    """
    if fetch is None:
        return None
    # Path 1: versions.json
    try:
        text, _ = fetch(_join(base, "versions.json"))
        names = json.loads(text)
    except Exception as exc:
        log.debug("docusaurus versions.json unavailable at %s: %s", base, exc)
        names = None
    if isinstance(names, list) and names:
        versions: dict[str, str] = {}
        for name in names:
            name = str(name)
            if name == "current":
                versions[name] = _join(base, "docs") + "/"
            else:
                versions[name] = _join(base, f"docs/{name}") + "/"
        return versions
    # Path 2: scrape the landing page for version links.
    try:
        html, _ = fetch(base if base.endswith("/") else base + "/")
    except Exception as exc:
        log.debug("docusaurus landing page unavailable at %s: %s", base, exc)
        return None
    parser = _VersionLinkParser()
    try:
        parser.feed(html)
    except Exception as exc:
        log.debug("docusaurus version-link parse failed for %s: %s", base, exc)
        return None
    versions = {}
    for href in parser.hrefs:
        m = _VERSION_PATH_RE.search(href)
        if m:
            ver = m.group(1)
            if ver and ver != "docs":
                versions[ver] = urljoin(base + "/", href)
    return versions or None


# ---------------------------------------------------------------------------
# 3. /latest/ redirect
# ---------------------------------------------------------------------------


def latest_redirect(base: str, *, fetch=None) -> str | None:
    """Resolve ``<base>/latest/`` following redirects.

    Returns the final URL when the redirect chain actually moves
    somewhere; ``None`` when ``/latest/`` does not exist or does not
    redirect (nothing to learn).
    """
    if fetch is None:
        return None
    target = _join(base, "latest") + "/"
    try:
        _, final_url = fetch(target)
    except Exception as exc:
        log.debug("latest redirect unavailable at %s: %s", target, exc)
        return None
    # Normalize trailing slashes before comparing: no movement, no info.
    if final_url.rstrip("/") == target.rstrip("/"):
        return None
    return final_url


# ---------------------------------------------------------------------------
# 4. git tags (no clone)
# ---------------------------------------------------------------------------


def _default_run(cmd, **kwargs):
    return subprocess.run(cmd, **kwargs)


def git_tags(repo_url: str, *, run=None) -> list[str] | None:
    """List tags via ``git ls-remote --tags`` (no clone).

    Returns the tag names in ls-remote order, de-duplicated (peeled
    ``^{}`` refs collapsed onto their tag). ``None`` when the repo has
    no tags or git fails.
    """
    run = run or _default_run
    try:
        proc = run(
            ["git", "ls-remote", "--tags", repo_url],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        log.debug("git ls-remote failed for %s: %s", repo_url, exc)
        return None
    if proc.returncode != 0:
        log.debug(
            "git ls-remote failed for %s: %s",
            repo_url,
            getattr(proc, "stderr", ""),
        )
        return None
    tags: list[str] = []
    seen: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        ref = parts[1]
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref[len("refs/tags/") :]
        if tag.endswith("^{}"):
            tag = tag[: -len("^{}")]
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags or None


# ---------------------------------------------------------------------------
# detect: try strategies in order against a library record
# ---------------------------------------------------------------------------


def detect(library_record, *, fetch=None, run=None) -> DetectionResult | None:
    """Try version-detection strategies in order (spec §6).

    ``library_record`` needs ``docs_url`` and ``repo_url`` attributes
    (the catalog ``Library`` model satisfies this; tests use a stub).
    """
    docs_url = getattr(library_record, "docs_url", None)
    repo_url = getattr(library_record, "repo_url", None)

    if docs_url:
        base = docs_url.rstrip("/")
        versions = sphinx_versions_json(base, fetch=fetch)
        if versions:
            return DetectionResult(strategy="sphinx_versions_json", versions=versions)
        versions = docusaurus_versions(base, fetch=fetch)
        if versions:
            return DetectionResult(strategy="docusaurus_versions", versions=versions)
        resolved = latest_redirect(base, fetch=fetch)
        if resolved:
            return DetectionResult(strategy="latest_redirect", resolved_url=resolved)

    if repo_url:
        tags = git_tags(repo_url, run=run)
        if tags:
            return DetectionResult(strategy="git_tags", tags=tags)

    return None
