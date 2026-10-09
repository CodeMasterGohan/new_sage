"""Tests for snapshots (in-memory fake + RustFS store with a fake client)."""

import pytest

from services.crawler.snapshots import (
    InMemorySnapshotStore,
    RustFSSnapshotStore,
    snapshot_key,
)


def test_snapshot_key_is_deterministic_and_safe():
    k1 = snapshot_key("My Lib!", "2.0.0", "https://docs.example.com/a b?x=1")
    k2 = snapshot_key("My Lib!", "2.0.0", "https://docs.example.com/a b?x=1")
    assert k1 == k2
    assert k1.startswith("snapshots/my_lib_/2.0.0/")
    assert k1.endswith(".html")
    assert " " not in k1 and "?" not in k1


def test_snapshot_key_differs_per_url():
    assert snapshot_key("l", "1", "https://x.test/a") != snapshot_key("l", "1", "https://x.test/b")


def test_inmemory_roundtrip():
    store = InMemorySnapshotStore()
    key = store.put_html("k/1.html", "<html>hi</html>")
    assert key == "k/1.html"
    assert store.get_html("k/1.html") == "<html>hi</html>"
    with pytest.raises(KeyError):
        store.get_html("missing")


class FakeRustFSClient:
    def __init__(self):
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[dict] = []

    def bucket_exists(self, bucket):
        return bucket in self.buckets

    def make_bucket(self, bucket):
        self.buckets.add(bucket)

    def put_object(self, bucket, key, data, length, content_type=None):
        body = data.read()
        assert len(body) == length
        self.objects[(bucket, key)] = body
        self.put_calls.append({"bucket": bucket, "key": key, "content_type": content_type})

    def get_object(self, bucket, key):
        body = self.objects[(bucket, key)]

        class Resp:
            def read(self):
                return body

            def close(self):
                pass

            def release_conn(self):
                pass

        return Resp()


def test_rustfs_store_put_and_get_with_fake_client():
    client = FakeRustFSClient()
    store = RustFSSnapshotStore(bucket="test-snaps", client=client)
    key = store.put_html("snapshots/l/1/a.html", "<html>hi</html>")
    assert key == "snapshots/l/1/a.html"
    assert "test-snaps" in client.buckets  # bucket auto-created
    assert client.put_calls[0]["content_type"] == "text/html; charset=utf-8"
    assert store.get_html(key) == "<html>hi</html>"


def test_rustfs_store_requires_endpoint_without_client():
    with pytest.raises(RuntimeError, match="RUSTFS_ENDPOINT"):
        RustFSSnapshotStore(endpoint="", client=None)
