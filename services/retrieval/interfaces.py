"""Injectable collaborator interfaces.

The Milvus collection code, the EmbeddingGemma 2 weights, the MiniLM
checkpoint, and the Postgres catalog are all sibling or deployment
concerns. This service codes against these interfaces only, so unit tests
run with fakes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence

from .models import ScoredHit, StoredChunk
from .predicates import VersionPredicate


class EmbeddingClient(ABC):
    """HTTP embedding-service boundary (EmbeddingGemma 2, spec §8).

    The service receives the RAW query text plus a task string; it applies
    the contractual PrefixContract prompt strings server-side. This client
    never prefixes text itself.
    """

    @abstractmethod
    def embed_query(self, text: str, task: str = "question_answering") -> list[float]:
        """Embed one query string for ``task``. Never downloads at runtime."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Expected vector width (768 for EmbeddingGemma 2)."""


class VectorStore(ABC):
    """Milvus ``sage_chunks`` access boundary (§9). Every method takes the
    SAME :class:`VersionPredicate` — no branch may widen the scope."""

    @abstractmethod
    def dense_search(
        self, vector: Sequence[float], predicate: VersionPredicate, top_k: int
    ) -> list[ScoredHit]:
        """ANN search over the 768-dim ``dense`` field."""

    @abstractmethod
    def bm25_search(
        self, query_text: str, predicate: VersionPredicate, top_k: int
    ) -> list[ScoredHit]:
        """Lexical search over the ``sparse`` BM25 field (raw query text;
        the collection's BM25 function analyzes it server-side)."""

    @abstractmethod
    def identifier_search(
        self, identifiers: Sequence[str], predicate: VersionPredicate, top_k: int
    ) -> list[ScoredHit]:
        """Literal match: chunks whose text/headings/url contain any of the
        identifier tokens (exact/alias library-name match and API-style
        tokens). Hits enter fusion with the identifier boost."""

    @abstractmethod
    def overview_chunks(
        self, predicate: VersionPredicate, top_k: int
    ) -> list[StoredChunk]:
        """Overview mode (no topic): top chunks from getting-started /
        overview pages; fallback to first chunks by the store's order."""


class Reranker(ABC):
    """Local CPU cross-encoder boundary (MiniLM, §9.1)."""

    model_id: str = "cross-encoder/ms-marco-MiniLM-L6-v2"

    @abstractmethod
    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """Score query/passage pairs. Raw scores only — never calibrated
        probabilities."""


@dataclass(frozen=True)
class LibraryRecord:
    """Catalog row for one library (spec §4)."""

    library_id: str
    name: str
    ecosystem: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedVersion:
    """Version resolution outcome (spec §9.2)."""

    version_id: str
    version: str  # the human-readable version string
    fallback: bool  # True when the request fell back to is_latest


class LibraryCatalog(ABC):
    """Postgres catalog boundary — version resolution and alias lookup.

    Implementations MUST NOT invent a catalog HTTP API; the two queries
    below are the whole surface.
    """

    @abstractmethod
    def get_library(self, library_id: str) -> LibraryRecord | None:
        """Return the library record, or None when unknown."""

    @abstractmethod
    def resolve_version(
        self, library_id: str, version: str | None
    ) -> ResolvedVersion:
        """Resolve a version string to a version_id.

        Exact match wins; unresolvable/None falls back to the ``is_latest``
        version with ``fallback=True``. Raises
        :class:`~errors.VersionResolutionError` when no version can be
        resolved at all.
        """
