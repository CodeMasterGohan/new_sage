"""Immutable raw upload and source-file storage in existing RustFS."""

from __future__ import annotations

import io
import os


class ImportStorage:
    def __init__(self, client=None, bucket: str | None = None) -> None:
        self.bucket = bucket or os.environ.get("RUSTFS_BUCKET", "sage-snapshots")
        if client is None:
            from minio import Minio

            endpoint = os.environ.get("RUSTFS_ENDPOINT")
            if not endpoint:
                raise RuntimeError("RUSTFS_ENDPOINT is required for manual imports")
            client = Minio(
                endpoint,
                access_key=os.environ.get("RUSTFS_ACCESS_KEY"),
                secret_key=os.environ.get("RUSTFS_SECRET_KEY"),
                secure=os.environ.get("RUSTFS_SECURE", "false").lower() == "true",
            )
        self.client = client

    def put(self, key: str, content: bytes, mime: str) -> str:
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)
        self.client.put_object(
            self.bucket,
            key,
            io.BytesIO(content),
            len(content),
            content_type=mime,
        )
        return key

    def get(self, key: str, *, max_bytes: int) -> bytes:
        response = self.client.get_object(self.bucket, key)
        try:
            content = response.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise ValueError("stored upload exceeds permitted size")
            return content
        finally:
            response.close()
            response.release_conn()
