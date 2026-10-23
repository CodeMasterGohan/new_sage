"""Worker ingestion must use the same configured collection as provisioning."""
from unittest.mock import Mock

import pytest

from services.ingestion.pipeline import MilvusVectorStore


def test_worker_connects_to_configured_collection(monkeypatch):
    pymilvus = pytest.importorskip('pymilvus')
    monkeypatch.setenv('MILVUS_COLLECTION', 'manual_upload_test')
    client = Mock()
    client.has_collection.side_effect = lambda name: name == 'manual_upload_test'
    monkeypatch.setattr(pymilvus, 'MilvusClient', lambda **kwargs: client)
    store = MilvusVectorStore(uri='http://test-fixture')
    assert store._connect() is client
    client.has_collection.assert_called_once_with('manual_upload_test')


def test_explicit_collection_overrides_environment(monkeypatch):
    monkeypatch.setenv('MILVUS_COLLECTION', 'from_environment')
    assert MilvusVectorStore(collection_name='explicit').collection_name == 'explicit'


def test_default_collection_without_environment(monkeypatch):
    monkeypatch.delenv('MILVUS_COLLECTION', raising=False)
    assert MilvusVectorStore().collection_name == 'sage_chunks'
