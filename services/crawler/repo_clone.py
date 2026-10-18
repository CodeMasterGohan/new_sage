"""Repo-clone version pinning (spec §9.3.1).

Preferred path when a library record has a ``repo_url``: tags are the
version — no detection heuristics needed. ``git clone --branch <tag>
--depth 1`` via subprocess (no GitPython dependency); Markdown needs no
extraction, so the checkout goes straight to the ingestion importers.

``clone_at_tag`` returns the commit SHA. ``find_doc_files`` collects
``README.md`` + ``docs/**/*.md`` + ``CHANGELOG.md`` (case variants) for
``services.ingestion.importers.import_markdown_files``.

The git runner is injectable (``run``) so tests never clone.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger("sage.crawler.repoclone")

_CLONE_TIMEOUT = 600  # seconds
_REV_PARSE_TIMEOUT = 30

# Tags become a `--branch` argument (list form, no shell), but validate
# anyway so a hostile tag can never become a git option or path escape.
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$")


class CloneError(Exception):
    """The clone (or tag resolution) failed."""


def _default_run(cmd, **kwargs):
    return subprocess.run(cmd, **kwargs)


def _check_tag(tag: str) -> str:
    if not _TAG_RE.match(tag) or tag.startswith("-"):
        raise CloneError(f"refusing to clone suspicious tag: {tag!r}")
    return tag


def clone_at_tag(
    repo_url: str, tag: str, dest: str | Path, *, run=None, timeout: int = _CLONE_TIMEOUT
) -> str:
    """``git clone --branch <tag> --depth 1 <repo_url> <dest>``.

    Returns the checkout's commit SHA. ``dest`` must not already exist
    (git clone refuses to clone into a non-empty directory).
    """
    run = run or _default_run
    tag = _check_tag(tag)
    dest = Path(dest)
    cmd = ["git", "clone", "--branch", tag, "--depth", "1", repo_url, str(dest)]
    log.info("cloning %s @ %s -> %s", repo_url, tag, dest)
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        raise CloneError(f"git clone failed for {repo_url}@{tag}: {exc}") from exc
    if proc.returncode != 0:
        raise CloneError(
            f"git clone failed for {repo_url}@{tag}: "
            f"{getattr(proc, 'stderr', '')}".strip()
        )
    try:
        rev = run(
            ["git", "-C", str(dest), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_REV_PARSE_TIMEOUT,
        )
    except Exception as exc:
        raise CloneError(f"rev-parse failed in {dest}: {exc}") from exc
    if rev.returncode != 0:
        raise CloneError(f"rev-parse failed in {dest}")
    sha = (rev.stdout or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise CloneError(f"rev-parse returned non-SHA: {sha!r}")
    log.info("cloned %s @ %s -> %s (%s)", repo_url, tag, dest, sha[:12])
    return sha


def find_doc_files(checkout: str | Path) -> list[Path]:
    """Collect doc Markdown files from a checkout (spec §9.3.1).

    ``README.md`` (repo root) + ``docs/**/*.md`` + ``CHANGELOG.md``,
    matching case-insensitively (``Readme.md``, ``Changelog.MD``, ...).
    Sorted for determinism. ``.git`` and other dot-directories are skipped.
    """
    checkout = Path(checkout)
    found: list[Path] = []
    for path in checkout.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() != ".md":
            continue
        if any(part.startswith(".") for part in path.relative_to(checkout).parts):
            continue
        name = path.name.lower()
        rel_parts = path.relative_to(checkout).parts
        if name == "readme.md" and len(rel_parts) == 1:
            found.append(path)
        elif name == "changelog.md" and len(rel_parts) == 1:
            found.append(path)
        elif len(rel_parts) > 1 and rel_parts[0].lower() == "docs":
            found.append(path)
    return sorted(found, key=lambda p: p.relative_to(checkout).as_posix())


def pages_from_clone(
    checkout: str | Path,
    *,
    library_id: str,
    version: str,
    source: str = "repoclone",
    repo_url: str | None = None,
    commit_sha: str | None = None,
) -> list:
    """Build ingestion ``Page`` objects from a checkout.

    Hands the file list from :func:`find_doc_files` to
    ``services.ingestion.importers.import_markdown_files`` and matches
    its ``Page`` shape exactly (import is lazy so this module stays
    importable without the sibling service on the path).
    """
    try:
        from services.ingestion.importers import import_markdown_files
    except ImportError as exc:
        raise RuntimeError(
            "services.ingestion is not importable: build the crawler image "
            "from the build root so sibling services are on sys.path."
        ) from exc
    files = find_doc_files(checkout)
    if not files:
        log.warning("no doc files found in %s", checkout)
    pages = import_markdown_files(
        files, library_id=library_id, version=version, source=source
    )
    # Clone-local paths are ephemeral and cannot serve as citations.
    # Use the exact immutable checkout SHA for verifiable source URLs.
    if repo_url and commit_sha:
        from urllib.parse import quote, urlsplit
        import re

        u = urlsplit(repo_url)
        repository = u.path.strip("/").removesuffix(".git")
        valid_repo = re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        valid_sha = re.fullmatch(r"[0-9a-fA-F]{40}", commit_sha)
        if u.scheme == "https" and valid_repo and valid_sha:
            if u.hostname == "github.com":
                base = f"https://github.com/{repository}/blob/{commit_sha}"
            elif u.hostname == "gitlab.com":
                base = f"https://gitlab.com/{repository}/-/blob/{commit_sha}"
            else:
                base = None
            if base:
                checkout = Path(checkout).resolve()
                for page in pages:
                    source_path = Path(page.metadata["repo_path"]).resolve()
                    rel = source_path.relative_to(checkout).as_posix()
                    page.url = base + "/" + quote(rel, safe="/")
                    page.metadata["source_commit"] = commit_sha
                    page.metadata["source_path"] = rel
    return pages
