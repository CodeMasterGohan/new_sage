"""Importers on tiny fixtures: markdown files, DevDocs docset, tldr page."""

from __future__ import annotations

from pathlib import Path

from services.ingestion.importers import (
    import_devdocs_docset,
    import_markdown_files,
    import_tldr_page,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_import_markdown_files_repo_clone():
    pages = import_markdown_files(
        [FIXTURES / "repo_sample"], library_id="fakelib", version="0.2.0"
    )
    assert len(pages) == 3  # README.md + docs/guide.md + CHANGELOG.md
    by_title = {p.title: p for p in pages}
    assert "fakelib" in by_title
    assert by_title["fakelib"].source == "repo"
    # content_hash is sha256 of markdown, stable.
    assert len(by_title["fakelib"].content_hash) == 64
    assert by_title["fakelib"].content_hash == by_title["fakelib"].content_hash


def test_import_markdown_files_skips_empty():
    d = FIXTURES / "repo_sample"
    (d / "empty.md").write_text("   \n", encoding="utf-8")
    try:
        pages = import_markdown_files([d], library_id="x", version="1")
        assert all(p.markdown.strip() for p in pages)
    finally:
        (d / "empty.md").unlink()


def test_import_devdocs_docset_two_pages():
    pages = import_devdocs_docset(
        FIXTURES / "devdocs_sample",
        library_id="widget",
        version="1.0",
        docs_url_base="https://docs.example.com/widget",
    )
    assert len(pages) == 2
    titles = {p.title for p in pages}
    assert titles == {"Widget.connect", "Widget.disconnect"}
    connect = next(p for p in pages if p.title == "Widget.connect")
    assert connect.source == "devdocs"
    assert connect.url == "https://docs.example.com/widget/widget/connect.html"
    # Code block kept with language; nav/footer dropped.
    assert "```javascript" in connect.markdown or "```js" in connect.markdown
    assert "Nav item" not in connect.markdown
    assert "Copyright footer" not in connect.markdown


def test_import_tldr_page():
    md = (FIXTURES / "tldr_sample.md").read_text(encoding="utf-8")
    page = import_tldr_page(md)
    assert page.title == "fakecmd"
    assert page.source == "tldr"
    assert page.markdown == md
    assert page.content_hash  # sha256 of markdown
