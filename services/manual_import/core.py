"""Offline-safe import archive validation and Markdown/HTML conversion.

The upload and worker share these guards. Archive members are never extracted
onto the filesystem; filenames are provenance only, not destination paths.
"""

from __future__ import annotations

import io
import re
import stat
import zipfile
from pathlib import PurePosixPath
from urllib.parse import quote

from services.ingestion.models import Page

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES = 1000
SUPPORTED_EXTENSIONS = frozenset({".md", ".markdown", ".html", ".htm"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class ImportValidationError(ValueError):
    """Upload is unsafe, too large, or contains no supported documents."""


def validate_identity(library: str, version: str) -> tuple[str, str]:
    library, version = library.strip(), version.strip()
    if not _IDENTIFIER.fullmatch(library) or not _IDENTIFIER.fullmatch(version):
        raise ImportValidationError(
            "library and version must be 1-100 characters: "
            "letters, numbers, dot, underscore or dash"
        )
    return library, version


def safe_path(raw: str) -> str:
    if "\\" in raw or ":" in raw or "\x00" in raw or any(ord(c) < 32 for c in raw):
        raise ImportValidationError("unsafe archive path")
    path = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("/")
        or len(raw) > 512
        or any(part in {"", ".", ".."} for part in raw.split("/"))
        or path.is_absolute()
    ):
        raise ImportValidationError("archive paths must be relative without . or .. segments")
    return path.as_posix()


def read_documents(archive: bytes) -> list[tuple[str, bytes]]:
    """Validate a ZIP and read only supported documents, with hard limits.

    Non-document assets are ignored, but unsafe members are rejected even if
    their extension is unsupported. Reject symlinks and duplicate names.
    """
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ImportValidationError("archive exceeds 64 MiB limit")
    result: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            entries = zf.infolist()
            if len(entries) > MAX_FILES * 5:
                raise ImportValidationError("too many archive members")
            for info in entries:
                name = safe_path(info.filename.rstrip("/") if info.is_dir() else info.filename)
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise ImportValidationError("symlinks are not allowed in archives")
                if info.is_dir():
                    continue
                total += info.file_size
                if total > MAX_TOTAL_BYTES or info.file_size > MAX_FILE_BYTES:
                    raise ImportValidationError("archive exceeds extracted size limits")
                if PurePosixPath(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                if name in seen:
                    raise ImportValidationError(f"duplicate document path: {name}")
                seen.add(name)
                if len(result) >= MAX_FILES:
                    raise ImportValidationError("archive exceeds 1000 document limit")
                # Reading with an explicit cap also guards against false ZIP size metadata.
                with zf.open(info) as member:
                    content = member.read(MAX_FILE_BYTES + 1)
                if len(content) > MAX_FILE_BYTES:
                    raise ImportValidationError(f"document exceeds 8 MiB limit: {name}")
                if len(content) > info.file_size:
                    raise ImportValidationError(f"invalid extracted size for: {name}")
                result.append((name, content))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, OSError) as exc:
        raise ImportValidationError("invalid, damaged, or encrypted ZIP archive") from exc
    if not result:
        raise ImportValidationError("no .md, .markdown, .html or .htm documents found")
    return sorted(result, key=lambda item: item[0])


def make_archive(files: list[tuple[str, bytes]]) -> bytes:
    """Create a canonical ZIP from uploaded files; validation is shared."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files:
            zf.writestr(safe_path(name), content)
    archive = buf.getvalue()
    read_documents(archive)
    return archive


def pages_from_documents(
    files: list[tuple[str, bytes]],
    library: str,
    version: str,
    *,
    snapshot_keys: dict[str, str] | None = None,
) -> list[Page]:
    """Produce ordinary SAGE pages and honest, stable local provenance."""
    from services.ingestion.html_to_md import html_to_markdown
    from services.ingestion.importers import _title_from_markdown

    pages = []
    for path, raw in files:
        url = (
            f"sage://manual/{quote(library, safe='')}/"
            f"{quote(version, safe='')}/{quote(path, safe='/')}"
        )
        text = raw.decode("utf-8-sig", errors="replace")
        markdown = (
            html_to_markdown(text, url)
            if PurePosixPath(path).suffix.lower() in {".html", ".htm"}
            else text
        )
        if not markdown.strip():
            continue
        pages.append(
            Page(
                library_id=library,
                version=version,
                url=url,
                title=_title_from_markdown(markdown, PurePosixPath(path).stem),
                markdown=markdown,
                source="manual",
                metadata={"source_path": path, "raw_snapshot_key": (snapshot_keys or {}).get(path)},
            )
        )
    if not pages:
        raise ImportValidationError("all imported documents were empty after conversion")
    return pages
