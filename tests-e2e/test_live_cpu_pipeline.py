"""CPU extraction/ingestion/search with real PostgreSQL and Milvus.

Vectors and token counts are deterministic fixtures, not model inference.
Requires disposable SAGE_TEST_DATABASE_URL and MILVUS_URI.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from services.catalog.repository import CatalogRepository
from services.catalog.writer import CatalogWriterAdapter
from services.ingestion.html_to_md import html_to_markdown
from services.ingestion.models import Page
from services.ingestion.pipeline import EmbeddingClient, MilvusVectorStore, ingest_pages
from services.milvus_setup.create_collection import Settings, ensure_collection
from services.retrieval.milvus_store import MilvusVectorStore as SearchStore
from services.retrieval.predicates import VersionPredicate


class FixtureEmbeddings(EmbeddingClient):
    def embed_chunks(self, chunks):
        return [[1.0] + [0.0] * 767 for _ in chunks]


def test_cpu_html_ingestion_and_version_scoped_live_search(monkeypatch):
    base_url = os.environ.get('SAGE_TEST_DATABASE_URL')
    uri = os.environ.get('MILVUS_URI')
    if not base_url or not uri:
        pytest.skip('requires disposable SAGE_TEST_DATABASE_URL and MILVUS_URI')
    pymilvus = pytest.importorskip('pymilvus')
    name = 'sage_cpu_' + uuid.uuid4().hex
    admin = create_engine(base_url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA {name}'))
    scoped = make_url(base_url).update_query_dict({'options': f'-csearch_path={name}'})
    url = scoped.render_as_string(hide_password=False)
    monkeypatch.setenv('DATABASE_URL', url)
    engine = create_engine(scoped)
    client = pymilvus.MilvusClient(uri=uri)
    writer = None
    sink = None
    search = None
    try:
        subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'],
            cwd=Path(__file__).resolve().parents[1] / 'services/catalog',
            check=True, capture_output=True, text=True)
        ensure_collection(Settings(uri=uri, collection=name, wait_timeout_s=20))
        writer = CatalogWriterAdapter(url, default_source='manual')
        sink = MilvusVectorStore(uri=uri, collection_name=name)
        for version in ('1.0', '2.0'):
            html = ('<html><body><article><h1>Quartz integration guide</h1>'
                    f'<p>Release {version} explains quartz document indexing. '
                    'This guide describes how to ingest documentation, preserve versions, '
                    'and retrieve the correct source passages from the search index.</p>'
                    '</article></body></html>')
            markdown = html_to_markdown(html, 'https://example.invalid/guide')
            assert 'quartz' in markdown.lower()
            page = Page(library_id=name, version=version, title='Quartz guide',
                url='https://example.invalid/guide', markdown=markdown, source='manual')
            report = ingest_pages(name, version, [page], source='manual',
                embedding_client=FixtureEmbeddings(), vector_store=sink, catalog=writer,
                token_len=lambda value: len(value.split()))
            assert report['chunks_indexed'] > 0 and not report['warnings'], report
        client.flush(name)
        with Session(engine) as session:
            repo = CatalogRepository(session)
            library = repo.resolve_library(name)
            assert repo.get_latest_version(library.id).version == '2.0'
        search = SearchStore(uri=uri, collection_name=name)
        for version in ('1.0', '2.0'):
            predicate = VersionPredicate(name, version)
            hits = search.bm25_search('quartz', predicate, 10)
            assert hits and all(hit.record.version_id == version for hit in hits)
            assert all(version in hit.record.text for hit in hits)
    finally:
        if search:
            search._client.close()
        if sink and sink._collection:
            sink._collection.close()
        if writer:
            writer._engine.dispose()
        if client.has_collection(name):
            client.drop_collection(name)
        client.close()
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA {name} CASCADE'))
        admin.dispose()
