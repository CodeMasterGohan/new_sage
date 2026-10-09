"""RustFS snapshot writer (spec §5.3, §11).

Raw HTML snapshots are the rebuild source if Milvus ever needs
reconstructing; the catalog ``page.raw_snapshot_key`` points at these
objects. Keys look like::

    snapshots/<library>/<version>/<slug>.html

``SnapshotStore`` is abstract; ``RustFSSnapshotStore`` is the real one
(``minio`` Python package, used unmodified against RustFS's S3-compatible
endpoint), ``InMemorySnapshotStore`` is the test fake.

Secrets never live in code: endpoint/keys come from ``RUSTFS_*``
environment variables (see README).
"""

from __future__ import annotations

import abc
import hashlib
import io
import logging
import os
import re
from urllib.parse import urlsplit

log = logging.getLogger("sage.crawler.snapshots")

BUCKET = "sage-snapshots"
CONTENT_TYPE = "text/html; charset=utf-8"


class SnapshotStore(abc.ABC):
    @abc.abstractmethod
    def put_html(self, key: str, html: str) -> str:
        """Store raw HTML; return the key (for ``page.raw_snapshot_key``)."""

    @abc.abstractmethod
    def get_html(self, key: str) -> str:
        """Read raw HTML back by key."""


def snapshot_key(library: str, version: str, url: str) -> str:
    """Deterministic, filesystem-safe snapshot key for a crawled URL."""
    slug = re.sub(r"[^a-z0-9]+", "-", urlsplit(url).path.strip("/").lower())
    slug = slug.strip("-") or "index"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    safe_lib = re.sub(r"[^a-z0-9_.-]+", "_", library.strip().lower())
    safe_ver = re.sub(r"[^a-z0-9_.-]+", "_", version.strip())
    return f"snapshots/{safe_lib}/{safe_ver}/{slug}-{digest}.html"


class RustFSSnapshotStore(SnapshotStore):
    """Real snapshot store backed by RustFS (S3-compatible)."""

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str | None = None,
        secure: bool | None = None,
        client=None,
    ) -> None:
        self.endpoint = endpoint or os.environ.get("RUSTFS_ENDPOINT", "")
        self.access_key = access_key or os.environ.get("RUSTFS_ACCESS_KEY", "")
        self.secret_key = secret_key or os.environ.get("RUSTFS_SECRET_KEY", "")
        self.bucket = bucket or os.environ.get("RUSTFS_BUCKET", BUCKET)
        if secure is None:
            secure = os.environ.get("RUSTFS_SECURE", "false").lower() == "true"
        self.secure = secure
        self._client = client
        if self._client is None:
            if not self.endpoint:
                raise RuntimeError(
                    "RUSTFS_ENDPOINT is not set: refusing to guess the "
                    "snapshot store."
                )
            try:
                from minio import Minio
            except ImportError as exc:
                raise RuntimeError(
                    "minio is not installed: install the pinned requirements "
                    "or use the Docker image."
                ) from exc
            self._client = Minio(
                self.endpoint,
                access_key=self.access_key or None,
                secret_key=self.secret_key or None,
                secure=self.secure,
            )

    def _ensure_bucket(self) -> None:
        if not self._client.bucket_exists(self.bucket):
            self._client.make_bucket(self.bucket)
            log.info("created snapshot bucket %s", self.bucket)

    def put_html(self, key: str, html: str) -> str:
        self._ensure_bucket()
        data = html.encode("utf-8")
        self._client.put_object(
            self.bucket,
            key,
            io.BytesIO(data),
            len(data),
            content_type=CONTENT_TYPE,
        )
        log.debug("snapshot stored: %s (%d bytes)", key, len(data))
        return key

    def get_html(self, key: str) -> str:
        resp = self._client.get_object(self.bucket, key)
        try:
            return resp.read().decode("utf-8")
        finally:
            resp.close()
            resp.release_conn()


class InMemorySnapshotStore(SnapshotStore):
    """Test fake: dict-backed, no network."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    def put_html(self, key: str, html: str) -> str:
        self.objects[key] = html
        return key

    def get_html(self, key: str) -> str:
        return self.objects[key]
