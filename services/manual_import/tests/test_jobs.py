"""Manual ingestion registers searchable immutable revisions without a crawl."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from services.catalog.models import Base, VersionSource
from services.catalog.repository import CatalogRepository
from services.ingestion.pipeline import FakeVectorStore, ingest_pages
from services.manual_import.core import make_archive
from services.manual_import.jobs import process_import_impl


class FakeStorage:
    def __init__(self):
        self.objects = {}

    def put(self, key, data, mime):
        self.objects[key] = (data, mime)
        return key


class FakeVectors:
    def delete_chunks(self, ids):
        return len(ids)


class FakeEmbedding:
    def embed_chunks(self, chunks):
        return [[0.01] * 768 for _ in chunks]


def test_import_creates_new_revision_and_excludes_refresh(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repo = CatalogRepository(session)
        storage = FakeStorage()

        def ingest(library, version, pages, source, *, catalog, vector_store):
            assert source == "manual"
            for page in pages:
                assert page.metadata["raw_snapshot_key"] in storage.objects
                catalog.upsert_page(page, chunk_count=0)
            catalog.activate_version(library, version)
            return {"pages_total": len(pages), "chunks_indexed": 0}

        archive = make_archive([("docs/install.md", b"# Install\nInstructions")])
        first = process_import_impl(
            "gitlab",
            "18.4",
            "one",
            archive,
            repo=repo,
            storage=storage,
            ingest=ingest,
            vector_store=FakeVectors(),
        )
        second = process_import_impl(
            "gitlab",
            "18.4",
            "two",
            archive,
            repo=repo,
            storage=storage,
            ingest=ingest,
            vector_store=FakeVectors(),
        )
        assert first["revision"] == 1
        assert second["revision"] == 2
        lib = repo.get_library_by_name("gitlab")
        rows = repo.list_versions(lib.id)
        assert len(rows) == 2
        assert all(
            row.status == "ready" and row.source == VersionSource.MANUAL.value for row in rows
        )
        assert repo.get_active_version(lib.id, "18.4").revision == 2
        assert not repo.list_stale_versions(now=dt.datetime.now(dt.UTC) + dt.timedelta(days=365))
        assert len(storage.objects) == 2  # raw files are immutable per upload


def test_real_chunk_and_vector_pipeline_replaces_removed_docs(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'full-pipeline.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        repo = CatalogRepository(session)
        storage = FakeStorage()
        vectors = FakeVectorStore()

        def ingest(library, version, pages, source, *, catalog, vector_store):
            return ingest_pages(
                library,
                version,
                pages,
                source,
                catalog=catalog,
                vector_store=vector_store,
                embedding_client=FakeEmbedding(),
                token_len=lambda text: len(text.split()),
            )

        first_archive = make_archive(
            [
                ("start.md", b"# Start\nInitial guide for GitLab CI"),
                ("remove.md", b"# Old\nThis page will be removed"),
            ]
        )
        first = process_import_impl(
            "gitlab",
            "18.4",
            "first",
            first_archive,
            repo=repo,
            storage=storage,
            ingest=ingest,
            vector_store=vectors,
        )
        assert first["ingest"]["chunks_indexed"] >= 2
        old_count = len(vectors.records)
        second_archive = make_archive([("start.md", b"# Start\nUpdated guide for GitLab CI")])
        second = process_import_impl(
            "gitlab",
            "18.4",
            "second",
            second_archive,
            repo=repo,
            storage=storage,
            ingest=ingest,
            vector_store=vectors,
        )
        assert second["ingest"]["pruned_orphans"] == old_count
        assert all("Old" not in rec["text"] for rec in vectors.records)
        assert any("Updated" in rec["text"] for rec in vectors.records)
