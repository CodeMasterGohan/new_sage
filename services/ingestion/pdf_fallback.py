"""PDF fallback parser (spec §5.4, §17.7).

Some projects only publish PDF manuals. This thin wrapper reuses
new_DRUID's pdftext path: page-text extraction -> each page becomes a
markdown-ish ``Page`` -> the SAME ``DocChunker`` pipeline handles it
(spec §7). olmOCR is NOT used here (no GPU requirement).

CPU-only. The pdftext import is lazy: a clear "pdftext not installed"
error is raised when it is missing (it is a pinned requirement and is
installed in the Docker image).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .models import Page


def _require_pdftext():
    try:
        from pdftext.extraction import dictionary_output
    except ImportError as exc:
        raise RuntimeError(
            "pdftext is not installed: the PDF fallback (spec §5.4) needs it. "
            "Install the pinned requirements (CPU-only, no model downloads) "
            "or use the Docker image."
        ) from exc
    return dictionary_output


def _page_text(raw_page: dict) -> str:
    """Join line texts in extraction order (defensive over pdftext shape)."""
    lines: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str) and text.strip():
                # Leaf text node (a line/char run).
                if "lines" not in node and "blocks" not in node:
                    lines.append(text.strip())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    # Prefer the structured path: blocks -> lines -> text.
    blocks = raw_page.get("blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            for line in block.get("lines", []) or []:
                if isinstance(line, dict) and line.get("text"):
                    lines.append(str(line["text"]).strip())
    if not lines:
        walk(raw_page)
    return "\n".join(t for t in lines if t)


def extract_pdf_pages(pdf_path: str | Path) -> list[str]:
    """Extract plain text per page via pdftext (CPU-only)."""
    dictionary_output = _require_pdftext()
    raw_pages = dictionary_output(str(pdf_path), sort=False, keep_chars=False)
    return [_page_text(p) for p in raw_pages]


def import_pdf(
    pdf: str | Path | bytes,
    *,
    library_id: str,
    version: str,
    url: str = "",
) -> list[Page]:
    """Turn a PDF manual into one ``Page`` per PDF page.

    Each page's text is treated as a markdown-ish page and flows through
    the same chunker as every other source. Default off unless a library
    has no HTML docs (spec §5.4).
    """
    if isinstance(pdf, bytes):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf)
        pdf_path = Path(tmp.name)
    else:
        pdf_path = Path(pdf)
    texts = extract_pdf_pages(pdf_path)
    pages: list[Page] = []
    for i, text in enumerate(texts):
        if not text.strip():
            continue
        pages.append(
            Page(
                library_id=library_id,
                version=version,
                url=f"{url}#page={i + 1}" if url else "",
                title=f"PDF page {i + 1}",
                markdown=text,
                source="pdf",
                metadata={"pdf_page": i + 1},
            )
        )
    return pages
