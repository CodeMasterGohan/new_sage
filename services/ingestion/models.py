"""Core data models for ingestion (spec §4, §5).

A ``Page`` is one ingested document page (an HTML doc page, a Markdown
file from a repo clone, a DevDocs entry, a tldr page, or one PDF page).
``Chunk`` is one retrieval unit derived from a page (spec §7).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_chunk_id(library_id: str, version: str, page_hash: str, text: str) -> str:
    """Stable, content-derived chunk ID (retries never duplicate)."""
    seed = f"{library_id}|{version}|{page_hash}|{text}"
    return "c_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:20]


def index_text_for_chunk(text: str, headings: list[str], identifiers: list[str]) -> str:
    """Compose the BM25 input text for a chunk (spec §9).

    The Milvus ``sage_chunks`` collection carries a server-side BM25
    function (``text_to_bm25``) that reads the ``text`` field and generates
    the ``sparse`` output field automatically — ingestion never writes
    ``sparse`` itself. So the ``text`` field inserted into Milvus must
    already contain all three BM25 channels: chunk text + headings +
    extracted code identifiers.

    Headings and identifiers are repeated to give them extra weight —
    API lookups are often exact-name searches.
    """
    head = " ".join(headings)
    idents = " ".join(identifiers)
    return f"{text}\n{head} {head}\n{idents} {idents} {idents}"


@dataclass
class Page:
    """One ingestible page.

    ``source`` is one of: repo | devdocs | mdn | tldr | crawl | pdf.
    ``content_hash`` (sha256 of ``markdown``) drives cross-version dedup
    (spec §6): unchanged pages are aliased, not re-chunked or re-embedded.
    """

    library_id: str
    version: str
    url: str
    title: str
    markdown: str
    source: str
    section_path: list[str] = field(default_factory=list)
    content_hash: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content_hash:
            self.content_hash = sha256_text(self.markdown)


@dataclass
class Chunk:
    """One retrieval unit (spec §7), ready for the ``sage_chunks`` schema."""

    chunk_id: str  # stable, content-derived
    library_id: str
    version_id: str  # version string (catalog's library_version PK)
    page_id: str  # content_hash of the source page
    url: str
    chunk_order: int  # neighbor ordering within the page
    text: str  # retrieval text (headings carried separately)
    token_count: int
    headings: list[str]  # breadcrumb, e.g. ["API Reference","Client","connect()"]
    code_language: str | None = None  # set when chunk is/contains a code block
    oversized_code: bool = False  # single code block >2000 tokens, emitted whole
    identifiers: list[str] = field(default_factory=list)  # code identifiers (BM25 boost)
    dense: list[float] = field(default_factory=list)  # embedding vector

    def to_milvus_record(self) -> dict:
        """Shape a Milvus ``sage_chunks`` insert record.

        Field layout (must match the milvus_setup schema):
        {chunk_id, library_id, version_id, dense, text,
         headings (JSON str), url, token_count, ordinal}.

        ``text`` carries the BM25 input composition (chunk text + headings
        + code identifiers, see :func:`index_text_for_chunk`): the
        collection's server-side BM25 function (``text_to_bm25``) reads
        ``text`` and generates the ``sparse`` field automatically, so the
        insert record must NOT include ``sparse``.

        ``ordinal`` carries ``chunk_order`` (the chunk's position within
        its page) so the retrieval service can order the overview-mode
        fallback deterministically (Wave 2 wiring fix).
        """
        import json

        return {
            "chunk_id": self.chunk_id,
            "library_id": self.library_id,
            "version_id": self.version_id,
            "dense": self.dense,
            "text": index_text_for_chunk(self.text, self.headings, self.identifiers),
            "headings": json.dumps(self.headings),
            "url": self.url,
            "token_count": self.token_count,
            "ordinal": self.chunk_order,
        }
