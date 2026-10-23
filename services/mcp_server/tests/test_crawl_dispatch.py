"""Exercise the MCP dispatch-to-worker identifier contract without network I/O."""
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from mcp_server.crawl_queue import dispatch_crawl
from services.catalog.models import Base
from services.catalog.repository import CatalogRepository
from services.crawler import jobs


@pytest.mark.parametrize('slug', ['react', 'REACT', 'reactjs'])
def test_dispatched_slug_can_be_consumed_by_crawl_worker(tmp_path, monkeypatch, slug):
    import redis
    import rq

    url = f'sqlite:///{tmp_path / "catalog.db"}'
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    monkeypatch.setenv('DATABASE_URL', url)
    monkeypatch.setenv('REDIS_URL', 'redis://test-fixture')
    connection = Mock()
    connection.set.return_value = True
    monkeypatch.setattr(redis, 'from_url', lambda _: connection)
    queued = Mock(id='queued-job')
    queue = Mock()
    queue.enqueue.return_value = queued
    monkeypatch.setattr(rq, 'Queue', lambda *a, **kw: queue)
    monkeypatch.setattr(jobs, '_crawl_web', lambda *a, **kw: {'pages': 1})
    try:
        with Session(engine) as session:
            repo = CatalogRepository(session)
            library = repo.create_library(name='react', aliases=['reactjs'],
                                          docs_url='https://react.dev')
            session.commit()
            assert dispatch_crawl(slug, '19.2.0') == 'queued-job'
            target, library_id, version = queue.enqueue.call_args.args
            assert target == 'services.crawler.jobs.crawl_library_version'
            result = jobs.crawl_library_version_impl(
                library_id, version, repo=repo, fetcher=Mock(), snapshots=Mock(), ingest=Mock())
            assert result['library'] == 'react'
            assert result['version'] == '19.2.0'
            assert library_id == str(library.id)
            connection.set.return_value = False
            assert dispatch_crawl(slug, '19.2.0') == 'already-queued'
            assert queue.enqueue.call_count == 1
            connection.set.return_value = True
            queue.enqueue.side_effect = RuntimeError('queue unavailable')
            with pytest.raises(RuntimeError, match='queue unavailable'):
                dispatch_crawl(slug, '19.2.0')
            connection.delete.assert_called_once_with(
                f'sage:pending-crawl:{library.id}:19.2.0')
            with pytest.raises(ValueError, match='unknown registered library'):
                dispatch_crawl('missing', '19.2.0')
            assert queue.enqueue.call_count == 2
            monkeypatch.delenv('DATABASE_URL')
            with pytest.raises(RuntimeError, match='DATABASE_URL is required'):
                dispatch_crawl(slug, '19.2.0')
    finally:
        engine.dispose()
