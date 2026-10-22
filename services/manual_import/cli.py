"""CLI for uploading a ZIP, Markdown/HTML file, or directory to SAGE admin."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .core import ImportValidationError, make_archive, read_documents, validate_identity


def archive_path(path: Path) -> bytes:
    if path.is_symlink():
        raise ImportValidationError("symlink sources are not supported")
    if path.is_file() and path.suffix.lower() == ".zip":
        archive = path.read_bytes()
        read_documents(archive)
        return archive
    if not path.exists():
        raise ImportValidationError(f"source does not exist: {path}")
    if path.is_file():
        return make_archive([(path.name, path.read_bytes())])
    if not path.is_dir():
        raise ImportValidationError("source must be a file or directory")
    docs = []
    for member in sorted(path.rglob("*")):
        if member.is_symlink():
            raise ImportValidationError(f"symlinks are not supported: {member}")
        if member.is_file() and member.suffix.lower() in {".md", ".markdown", ".html", ".htm"}:
            docs.append((member.relative_to(path).as_posix(), member.read_bytes()))
    return make_archive(docs)


def request_json(
    url: str, token: str, *, body: bytes | None = None, content_type: str | None = None
) -> dict:
    headers = {"Authorization": "Bearer " + token}
    if content_type:
        headers["Content-Type"] = content_type
    request = Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urlopen(request, timeout=120) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(
            f"SAGE API returned HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"SAGE API could not be reached: {exc.reason}") from exc


def multipart_upload(library: str, version: str, archive: bytes) -> tuple[bytes, str]:
    boundary = "sage-" + uuid.uuid4().hex
    body = bytearray()
    for key, value in (("library", library), ("version", version)):
        body.extend(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
            ).encode()
        )
    body.extend(
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="documentation.zip"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode()
    )
    body.extend(archive)
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import offline documentation into SAGE")
    parser.add_argument("path", type=Path, help="Markdown/HTML file, ZIP, or folder")
    parser.add_argument("--library", required=True, help="e.g. gitlab")
    parser.add_argument("--version", required=True, help="e.g. 18.x")
    parser.add_argument("--url", default="http://127.0.0.1:8012", help="admin API base URL")
    parser.add_argument("--wait", action="store_true", help="poll until processing finishes")
    args = parser.parse_args(argv)
    token = os.environ.get("SAGE_ADMIN_TOKEN", "")
    if len(token) < 32:
        parser.error("set SAGE_ADMIN_TOKEN in the environment (32+ chars)")
    try:
        library, version = validate_identity(args.library, args.version)
        archive = archive_path(args.path)
        body, content_type = multipart_upload(library, version, archive)
        base = args.url.rstrip("/")
        result = request_json(base + "/admin/imports", token, body=body, content_type=content_type)
        print(json.dumps(result, indent=2))
        if args.wait:
            while True:
                time.sleep(3)
                status = request_json(base + result["status_url"], token)
                if status["status"] in {"finished", "failed", "stopped", "canceled"}:
                    print(json.dumps(status, indent=2))
                    return 0 if status["status"] == "finished" else 1
    except (ImportValidationError, RuntimeError, OSError) as exc:
        print(f"SAGE import error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
