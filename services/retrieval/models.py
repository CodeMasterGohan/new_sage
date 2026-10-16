"""Data models: stored chunks, scored hits, and the POST /retrieve contract.

The Milvus collection is ``sage_chunks`` (fields dictated in the module
docstring of :mod:`milvus_store`); ``dense``/``sparse`` vectors never leave
the store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StoredChunk:
    """One Milvus ``sage_chunks`` record as returned by the vector store."""

    chunk_id: str
    library_id: str
    version_id: str
    text: str
    headings: tuple[str, ...] = ()  # JSON breadcrumb, e.g. ("API Reference", "Client", "connect()")
    url: str = ""
    token_count: int = 0


@dataclass(frozen=True)
class ScoredHit:
    """A stored chunk with its 1-based rank inside one retrieval branch."""

    record: StoredChunk
    rank: int  # 1-based rank within the branch that produced this hit
    score: float | None = None  # branch-native score, if the store provides one


@dataclass(frozen=True)
class FusedCandidate:
    """A deduplicated candidate with its RRF fusion score."""

    record: StoredChunk
    fusion_score: float
    branches: tuple[str, ...] = ()  # which branches contributed, e.g. ("dense", "bm25")


# Matches a fenced code block's language tag: ```python, ```js, ```c++ ...
_FENCE_RE = re.compile(r"^```([A-Za-z0-9_+#.\-]+)", re.MULTILINE)


def detect_code_language(text: str) -> str | None:
    """Infer the passage's code language from its first fenced code block.

    The ``sage_chunks`` schema carries no code_language column, so this is
    derived at render time (ingestion preserves fenced blocks verbatim with
    their language tags, §5.3). Returns None when no fenced block is found.
    """
    match = _FENCE_RE.search(text or "")
    return match.group(1).lower() if match else None


def estimate_tokens(text: str) -> int:
    """Fallback token estimate when a chunk has no stored token_count."""
    return max(1, len(text or "") // 4)


@dataclass
class Passage:
    """One rendered passage in the POST /retrieve response."""

    text: str
    url: str
    section: list[str]
    code_language: str | None
    score: float | None  # MiniLM reranker score; None in overview mode
    token_count: int = 0  # internal: used for token-budget truncation

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "url": self.url,
            "section": list(self.section),
            "code_language": self.code_language,
            "score": self.score,
        }


@dataclass
class RetrieveResponse:
    """The POST /retrieve result contract, exactly."""

    library: dict
    version: str
    version_fallback: bool
    passages: list[Passage] = field(default_factory=list)
    diagnostics: dict | None = None  # opt-in internal retrieval trace

    def to_dict(self) -> dict:
        result = {
            "library": dict(self.library),
            "version": self.version,
            "version_fallback": self.version_fallback,
            "passages": [p.to_dict() for p in self.passages],
        }
        if self.diagnostics is not None:
            result["diagnostics"] = self.diagnostics
        return result
