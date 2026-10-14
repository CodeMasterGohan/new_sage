"""Seed-corpus importers (spec §5.1, §9.3.1).

Each importer turns a local source into ``Page`` objects — no network, no
crawling. Kept dependency-light and unit-testable on tiny fixtures.

- ``import_markdown_files`` — repo clones (README.md + docs/**/*.md +
  CHANGELOG.md, spec §9.3.1). Markdown needs no extraction.
- ``import_devdocs_docset`` — a DevDocs docset directory: index.json/db.json
  + HTML files, each page converted with ``html_to_markdown``. This is the
  parse side of the §5.1.1 monthly sync (pull/diff of the DevDocs image is
  owned by the refresh job, not this importer).
- ``import_tldr_page`` — one tldr-pages Markdown page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .html_to_md import html_to_markdown
from .models import Page


def _title_from_markdown(text: str, fallback: str) -> str:
    for line in text.splitlines():
        m = re.match(r"^#{1,3}\s+(.*?)\s*#*\s*$", line.strip())
        if m:
            return m.group(1).strip()
    return fallback


def import_markdown_files(
    paths: list[str | Path],
    *,
    library_id: str,
    version: str,
    source: str = "repo",
) -> list[Page]:
    """Import Markdown files from repo clones (spec §9.3.1).

    ``paths`` may be files or directories (searched recursively for
    ``*.md``). Output is sorted by relative path for determinism.
    """
    files: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files.extend(sorted(p.rglob("*.md"), key=lambda f: f.as_posix()))
        elif p.is_file():
            files.append(p)
    pages: list[Page] = []
    for f in sorted(files, key=lambda f: f.as_posix()):
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = f.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            continue
        pages.append(
            Page(
                library_id=library_id,
                version=version,
                url="",  # repo-sourced pages have no web URL; path in metadata
                title=_title_from_markdown(text, f.stem),
                markdown=text,
                source=source,
                metadata={"repo_path": str(f)},
            )
        )
    return pages


def _load_docset_index(docset_dir: Path) -> list[dict]:
    """Read the DevDocs page index (index.json, falling back to db.json)."""
    for name in ("index.json", "db.json"):
        p = docset_dir / name
        if not p.is_file():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "entries" in data:
            data = data["entries"]
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict) and e.get("path")]
    return []


def import_devdocs_docset(
    docset_dir: str | Path,
    *,
    library_id: str,
    version: str,
    docs_url_base: str = "",
) -> list[Page]:
    """Parse a DevDocs docset directory into pages (spec §5.1.1).

    Layout: ``index.json`` (or ``db.json``) listing entries with ``name``
    and ``path``, plus one HTML file per entry. Each page's HTML is
    converted with :func:`html_to_markdown`.
    """
    docset_dir = Path(docset_dir)
    entries = _load_docset_index(docset_dir)
    pages: list[Page] = []
    for entry in entries:
        rel = str(entry["path"]).lstrip("/")
        html_path = docset_dir / rel
        if not html_path.is_file():
            html_path = docset_dir / (rel + ".html")
        if not html_path.is_file():
            continue
        html = html_path.read_text(encoding="utf-8", errors="replace")
        url = f"{docs_url_base.rstrip('/')}/{rel}" if docs_url_base else rel
        markdown = html_to_markdown(html, url)
        if not markdown.strip():
            continue
        pages.append(
            Page(
                library_id=library_id,
                version=version,
                url=url,
                title=str(entry.get("name", "")) or rel,
                markdown=markdown,
                source="devdocs",
                metadata={"docset_path": rel, "doc_type": entry.get("type", "")},
            )
        )
    return pages


def import_tldr_page(
    md_text: str,
    *,
    library_id: str = "tldr",
    version: str = "latest",
    command: str | None = None,
) -> Page:
    """Import one tldr-pages Markdown page.

    tldr pages are already Markdown; the title is the page's H1 (the
    command name), overridable via ``command``.
    """
    title = command or _title_from_markdown(md_text, "tldr")
    return Page(
        library_id=library_id,
        version=version,
        url=f"https://tldr.sh/{title}" if title else "",
        title=title,
        markdown=md_text,
        source="tldr",
        metadata={"command": title},
    )
