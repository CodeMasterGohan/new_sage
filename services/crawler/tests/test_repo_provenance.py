"""Citations for cloned docs must identify the immutable source file."""
from services.crawler.repo_clone import pages_from_clone


SHA = "a" * 40


def test_pinned_github_doc_urls(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("# Homepage\n")
    (tmp_path / "docs" / "api reference.md").write_text("# API\n")
    pages = pages_from_clone(
        tmp_path, library_id="pkg", version="2.0.0",
        repo_url="https://github.com/example/package.git", commit_sha=SHA,
    )
    urls = {p.url for p in pages}
    assert f"https://github.com/example/package/blob/{SHA}/README.md" in urls
    assert f"https://github.com/example/package/blob/{SHA}/docs/api%20reference.md" in urls
    assert all(p.metadata.get("source_commit") == SHA for p in pages)


def test_pinned_gitlab_doc_urls(tmp_path):
    (tmp_path / "README.md").write_text("# Docs\n")
    pages = pages_from_clone(
        tmp_path, library_id="pkg", version="2.0.0",
        repo_url="https://gitlab.com/example/package", commit_sha=SHA,
    )
    assert pages[0].url == f"https://gitlab.com/example/package/-/blob/{SHA}/README.md"


def test_invalid_source_metadata_never_creates_fake_citation(tmp_path):
    (tmp_path / "README.md").write_text("# Docs\n")
    pages = pages_from_clone(
        tmp_path, library_id="pkg", version="2.0.0",
        repo_url="https://internal.invalid/secret", commit_sha=SHA,
    )
    assert pages[0].url == ""
