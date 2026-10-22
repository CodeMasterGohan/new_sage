"""Manual import parsing and archive threat-model tests (offline)."""

from __future__ import annotations

import io
import zipfile

import pytest

from services.manual_import.cli import archive_path, multipart_upload
from services.manual_import.core import (
    ImportValidationError,
    make_archive,
    pages_from_documents,
    read_documents,
    validate_identity,
)


def test_markdown_folder_and_provenance(tmp_path):
    docs = tmp_path / "docs"
    (docs / "ci").mkdir(parents=True)
    (docs / "ci" / "pipelines.md").write_text("# GitLab CI\nHow to write a pipeline")
    (docs / "README.md").write_text("# Overview")
    archive = archive_path(docs)
    files = read_documents(archive)
    assert [path for path, _ in files] == ["README.md", "ci/pipelines.md"]
    pages = pages_from_documents(files, "gitlab", "18.4")
    assert pages[1].title == "GitLab CI"
    assert pages[1].url == "sage://manual/gitlab/18.4/ci/pipelines.md"
    assert pages[1].source == "manual"


def test_html_and_markdown_share_pipeline(monkeypatch):
    import services.ingestion.html_to_md as html_module

    monkeypatch.setattr(html_module, "html_to_markdown", lambda html, url: "# Converted\n" + html)
    archive = make_archive([("install.html", b"<h1>Install</h1>"), ("guide.md", b"# Guide")])
    pages = pages_from_documents(read_documents(archive), "gitlab", "18.x")
    assert [p.title for p in pages] == ["Guide", "Converted"]
    assert "<h1>Install</h1>" in pages[1].markdown


@pytest.mark.parametrize("name", ["../evil.md", "/tmp/evil.md", "a/../../evil.md", "a\\evil.md"])
def test_reject_unsafe_paths(name):
    with pytest.raises(ImportValidationError):
        make_archive([(name, b"test")])


def test_reject_duplicate_paths():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("guide.md", "# One")
        zf.writestr("guide.md", "# Two")
    with pytest.raises(ImportValidationError, match="duplicate"):
        read_documents(buf.getvalue())


def test_reject_large_expanded_doc():
    archive = make_archive([("readme.md", b"hello")])
    assert read_documents(archive)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.md", b"A" * (8 * 1024 * 1024 + 1))
    with pytest.raises(ImportValidationError, match="size"):
        read_documents(buf.getvalue())


def test_only_unsupported_files_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("image.png", b"PNG")
    with pytest.raises(ImportValidationError, match="no .md"):
        read_documents(buf.getvalue())


def test_identifier_and_multipart():
    assert validate_identity("gitlab", "18.4") == ("gitlab", "18.4")
    with pytest.raises(ImportValidationError):
        validate_identity("../../db", "18")
    body, content_type = multipart_upload("gitlab", "18.4", b"ZIPDATA")
    assert content_type.startswith("multipart/form-data; boundary=")
    assert b"ZIPDATA" in body
    assert b'name="files"' in body



@pytest.mark.parametrize("name", ["C:/Windows/win.ini", "C:secret.md", "file://etc/passwd"])
def test_reject_drive_and_scheme_paths(name):
    with pytest.raises(ImportValidationError):
        make_archive([(name, b"test")])
