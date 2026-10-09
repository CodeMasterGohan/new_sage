"""Live Milvus provisioning tests.

Requires a reachable Milvus: set ``MILVUS_URI`` (e.g.
``http://localhost:19531``). Skipped in CI and in offline runs — the unit
tests (test_schema.py, test_deletion.py) need no live server.

These tests create and DROP a scratch collection
(``MILVUS_TEST_COLLECTION`` or ``sage_chunks_test``); never point them at a
production ``sage_chunks``.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("MILVUS_URI"),
    reason="MILVUS_URI not set — live Milvus tests are skipped",
)

pytest.importorskip("pymilvus", reason="pymilvus not installed")

from pymilvus import MilvusClient  # noqa: E402

from .. import schema  # noqa: E402
from ..create_collection import (  # noqa: E402
    Settings,
    ensure_collection,
    verify_schema_compatible,
)

TEST_COLLECTION = os.environ.get("MILVUS_TEST_COLLECTION", "sage_chunks_test")


@pytest.fixture()
def client():
    c = MilvusClient(uri=os.environ["MILVUS_URI"])
    yield c
    if c.has_collection(TEST_COLLECTION):
        c.drop_collection(TEST_COLLECTION)
    c.close()


def test_provision_creates_collection_with_schema(client):
    ensure_collection(Settings(uri=os.environ["MILVUS_URI"], collection=TEST_COLLECTION))
    assert client.has_collection(TEST_COLLECTION)
    verify_schema_compatible(client, TEST_COLLECTION)


def test_provision_is_idempotent(client):
    settings = Settings(uri=os.environ["MILVUS_URI"], collection=TEST_COLLECTION)
    ensure_collection(settings)
    ensure_collection(settings)  # second run must not raise
    desc = client.describe_collection(TEST_COLLECTION)
    assert {f["name"] for f in desc["fields"]} == set(schema.field_names())


def test_indexes_present_after_provision(client):
    ensure_collection(Settings(uri=os.environ["MILVUS_URI"], collection=TEST_COLLECTION))
    by_field = {i["field_name"]: i for i in client.list_indexes(TEST_COLLECTION)}
    assert "dense" in by_field and "sparse" in by_field
