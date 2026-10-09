"""CatalogWriterAdapter tests (Wave 2 wiring).

The adapter implements the ingestion pipeline's ``CatalogWriter`` protocol
over ``CatalogRepository``. Tests run on SQLite (the models fall back to
JSON for TEXT[] there) with duck-typed page/chunk stubs — no ingestion
package import needed, mirroring the production dependency direction.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.catalog import (
    CatalogRepository,
    make_catalog_writer,
)
from services.catalog.models import Base


class _Page:
    def __init__(self, library_id, version, url, title, content_hash):
        self.library_id = library_id
        self.version = version
        self.url = url
        self.title = title
        self.content_hash = content_hash
        self.metadata = {}


class _Chunk:
    def __init__(self, library_id, version, page_hash, order, text="t"):
        self.chunk_id = f"c_{order}"
        self.library_id = library_id
        self.version_id = version  # ingestion writes the version string
        self.page_id = page_hash  # ingestion Chunk.page_id = content_hash
        self.url = "https://docs.example.com/p"
        self.chunk_order = order
        self.text = text
        self.token_count = 5
        self.headings = ["Guide"]


@pytest.fixture()
def writer(tmp_path):
    db = tmp_path / "writer.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return make_catalog_writer(f"sqlite:///{db}")


@pytest.fixture()
def repo(writer, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'writer.db'}")
    with Session(engine) as session:
        yield CatalogRepository(session)


def test_make_catalog_writer_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        make_catalog_writer()


def test_upsert_page_and_insert_chunks(writer, repo):
    page = _Page("fakelib", "1.0.0", "https://docs.example.com/p", "P", "h1")
    assert writer.page_hashes("fakelib", "1.0.0") == set()

    writer.upsert_page(page, chunk_count=2)
    # upsert is idempotent on (version, content_hash)
    writer.upsert_page(page, chunk_count=2)

    chunks = [_Chunk("fakelib", "1.0.0", "h1", 0), _Chunk("fakelib", "1.0.0", "h1", 1)]
    assert writer.insert_chunks(chunks) == 2
    # page_hashes reports the searchable index: activate first.
    writer.activate_version("fakelib", "1.0.0")
    assert writer.page_hashes("fakelib", "1.0.0") == {"h1"}

    lib = repo.resolve_library("fakelib")
    assert lib is not None
    ver = repo.get_version_by_string(lib.id, "1.0.0")
    pages = repo.list_pages(ver.id)
    assert len(pages) == 1  # no duplicate page row
    assert pages[0].content_hash == "h1"
    rows = repo.list_chunks(pages[0].id)
    assert [r.ordinal for r in rows] == [0, 1]
    assert rows[0].headings == ["Guide"]


def test_alias_page_records_page_without_chunks(writer, repo):
    page = _Page("fakelib", "1.0.0", "https://docs.example.com/p", "P", "h1")
    writer.upsert_page(page, chunk_count=1)
    writer.insert_chunks([_Chunk("fakelib", "1.0.0", "h1", 0)])

    writer.alias_page("fakelib", "1.0.0", "1.0.1", page)

    lib = repo.resolve_library("fakelib")
    new_ver = repo.get_version_by_string(lib.id, "1.0.1")
    assert new_ver is not None
    pages = repo.list_pages(new_ver.id)
    assert len(pages) == 1
    assert pages[0].content_hash == "h1"
    assert repo.list_chunks(pages[0].id) == []  # not re-chunked
    writer.activate_version("fakelib", "1.0.1")
    assert writer.page_hashes("fakelib", "1.0.1") == {"h1"}


def test_activate_version_marks_ready_and_latest(writer, repo):
    page = _Page("fakelib", "1.0.0", "https://docs.example.com/p", "P", "h1")
    writer.upsert_page(page, chunk_count=0)
    writer.activate_version("fakelib", "1.0.0")

    lib = repo.resolve_library("fakelib")
    ver = repo.get_version_by_string(lib.id, "1.0.0")
    assert ver.status == "ready"
    assert ver.is_latest is True
    assert repo.get_latest_version(lib.id).id == ver.id


def test_page_hashes_unknown_library_fails_open(writer):
    assert writer.page_hashes("nope", "1.0.0") == set()
