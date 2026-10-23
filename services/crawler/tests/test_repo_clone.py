"""Tests for repo_clone (no real clones: injected run; real temp dirs)."""

from pathlib import Path

import pytest

from services.crawler.repo_clone import (
    CloneError,
    clone_at_tag,
    find_doc_files,
    pages_from_clone,
)


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


SHA = "c" * 40


def run_ok(cmd, **kwargs):
    if "clone" in cmd:
        return FakeProc(0, stdout="")
    if "rev-parse" in cmd:
        return FakeProc(0, stdout=SHA + "\n")
    raise AssertionError(f"unexpected command: {cmd}")


def test_clone_at_tag_issues_git_clone_and_returns_sha(tmp_path):
    seen = []

    def run(cmd, **kwargs):
        seen.append(cmd)
        return run_ok(cmd, **kwargs)

    dest = tmp_path / "repo"
    sha = clone_at_tag("https://github.com/acme/widget", "v2.0.0", dest, run=run)
    assert sha == SHA
    assert seen[0] == [
        "git", "clone", "--branch", "v2.0.0", "--depth", "1",
        "https://github.com/acme/widget", str(dest),
    ]
    assert seen[1][:3] == ["git", "-C", str(dest)]


def test_clone_at_tag_rejects_suspicious_tag(tmp_path):
    with pytest.raises(CloneError):
        clone_at_tag("https://github.com/acme/widget", "v1; rm -rf /", tmp_path / "x",
                     run=run_ok)
    with pytest.raises(CloneError):
        clone_at_tag("https://github.com/acme/widget", "--upload-pack=evil",
                     tmp_path / "x", run=run_ok)


def test_clone_at_tag_surfaces_git_failure(tmp_path):
    def run(cmd, **kwargs):
        return FakeProc(128, stderr="tag not found")

    with pytest.raises(CloneError, match="tag not found"):
        clone_at_tag("https://github.com/acme/widget", "v9.9.9", tmp_path / "x", run=run)


def _write(root: Path, rel: str, text: str = "x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_find_doc_files_collects_readme_docs_changelog(tmp_path):
    root = tmp_path / "repo"
    _write(root, "README.md", "# Widget")
    _write(root, "Readme.txt", "not markdown")  # wrong suffix: excluded
    _write(root, "CHANGELOG.MD", "# changes")
    _write(root, "docs/guide.md", "# guide")
    _write(root, "docs/sub/advanced.md", "# advanced")
    _write(root, "Docs/upper.md", "# upper")  # case-insensitive docs dir
    _write(root, "src/code.md", "# code")  # not under docs/: excluded
    _write(root, ".git/hooks/note.md", "# hook")  # dot-dir: excluded

    found = find_doc_files(root)
    rels = [p.relative_to(root).as_posix() for p in found]
    assert len(rels) == 5
    assert sorted(r.lower() for r in rels) == [
        "changelog.md",
        "docs/guide.md",
        "docs/sub/advanced.md",
        "docs/upper.md",
        "readme.md",
    ]
    assert found == sorted(found, key=lambda p: p.relative_to(root).as_posix())


def test_gitlab_singular_doc_directory_is_discovered(tmp_path):
    root = tmp_path / 'gitlab'
    expected = _write(root, 'doc/ci/yaml/_index.md', '# GitLab CI/CD YAML reference')
    _write(root, 'doc/.hidden/private.md', 'excluded')
    _write(root, 'src/internal.md', 'excluded')
    assert find_doc_files(root) == [expected]


def test_pages_from_clone_matches_importer_page_shape(tmp_path):
    root = tmp_path / "repo"
    _write(root, "README.md", "# Widget\n\nDoes things.")
    _write(root, "docs/guide.md", "# Guide\n\nHow to.")

    pages = pages_from_clone(root, library_id="lib-1", version="2.0.0")
    assert len(pages) == 2
    page = pages[0]
    # Shape matches services.ingestion.importers.Page exactly.
    assert page.library_id == "lib-1"
    assert page.version == "2.0.0"
    assert page.source == "repoclone"
    assert page.title == "Widget"
    assert page.content_hash  # sha256 of markdown, auto-computed
    assert page.metadata["repo_path"].endswith("README.md")
