"""Ingestion pipeline: pages -> chunks -> embeddings -> Milvus -> catalog.

Stage flow for one (library, version) ingestion (new_DRUID §12 shape,
minus tenancy and PDF routing)::

    QUEUED -> DEDUP -> CHUNKING -> EMBEDDING -> INDEXING -> READY
    failures -> FAILED (RQ retries the job per queue policy)

``ingest_pages`` is directly callable (tests, local runs); in production
it runs inside the RQ worker (``worker.py``), so all arguments must be
picklable — injectables (embedding client, vector store, catalog writer)
are constructed lazily when not supplied.

Seams (owned by sibling agents):
* ``VectorStore`` — Milvus ``sage_chunks``; :class:`MilvusVectorStore`
  (pymilvus, lazy import) is the real one, :class:`FakeVectorStore` for tests.
* Embedding backend — :class:`HttpEmbeddingClient` posts to the embedding
  service (``EMBEDDING_URL`` + ``/embed_documents``); tests inject a fake.
* Catalog — the sibling catalog package (see :func:`load_catalog_writer`).

Chunk IDs are content-derived (stable), so retries never duplicate
(idempotency).
"""

from __future__ import annotations

import abc
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import requests

from .doc_chunker import ChunkerSettings, DocChunker
from .models import Chunk, Page, content_chunk_id

log = logging.getLogger("sage.ingestion")

EMBEDDING_DIMENSION = 768  # EmbeddingGemma 2 (spec §8)
INGEST_QUEUE = "sage-ingest"
DEFAULT_EMBED_BATCH_SIZE = 128
DEFAULT_MILVUS_UPSERT_BATCH_SIZE = 500


class _VectorReuseCache:
    """Bounded in-process LRU cache of (library_id, page_hash, chunk_order, text) -> dense vector.

    Used during cross-version aliasing (§6) when the target VectorStore
    instance does not already hold the previous version's records in memory
    (e.g. isolated store instances in unit tests). Production primarily
    fetches existing vectors from Milvus via ``vector_store.fetch_vectors``.
    """

    def __init__(self, max_entries: int = 16384) -> None:
        self._max = max_entries
        self._data: OrderedDict[tuple[str, str, int, str], list[float]] = OrderedDict()

    def get(self, library_id: str, page_hash: str, order: int, text: str) -> list[float] | None:
        key = (library_id, page_hash, order, text)
        vec = self._data.get(key)
        if vec is not None:
            self._data.move_to_end(key)
            return list(vec)
        return None

    def put(self, library_id: str, page_hash: str, order: int, text: str, vec: list[float]) -> None:
        if not vec:
            return
        key = (library_id, page_hash, order, text)
        self._data[key] = list(vec)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)


_REUSE_CACHE = _VectorReuseCache()


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@dataclass
class IngestReport:
    library_id: str
    version: str
    pages_total: int = 0
    pages_aliased: int = 0  # unchanged vs prev_version: dedup (§6)
    pages_chunked: int = 0
    chunks_indexed: int = 0
    oversized_code_chunks: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "library_id": self.library_id,
            "version": self.version,
            "pages_total": self.pages_total,
            "pages_aliased": self.pages_aliased,
            "pages_chunked": self.pages_chunked,
            "chunks_indexed": self.chunks_indexed,
            "oversized_code_chunks": self.oversized_code_chunks,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# embedding client
# ---------------------------------------------------------------------------


class EmbeddingClient(abc.ABC):
    @abc.abstractmethod
    def embed_chunks(self, chunks: list[Chunk]) -> list[list[float]]:
        """Return one dense vector per chunk, in order."""


class HttpEmbeddingClient(EmbeddingClient):
    """POST chunks to the embedding service (spec §8).

    The service applies the EmbeddingGemma 2 PrefixContract server-side
    (``web_search_document`` for chunks). Chunks carrying code are sent
    with ``is_code=true``. Requests are batched and deduplicated per batch
    so large library versions do not time out or OOM the HTTP endpoint.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int = 120,
        batch_size: int | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("EMBEDDING_URL", "")).rstrip("/")
        if not self.base_url:
            raise RuntimeError(
                "EMBEDDING_URL is not set: refusing to guess the embedding "
                "endpoint."
            )
        self.timeout = timeout
        raw_bs = batch_size or int(
            os.environ.get("EMBEDDING_CLIENT_BATCH_SIZE", str(DEFAULT_EMBED_BATCH_SIZE))
        )
        self.batch_size = max(1, raw_bs)

    def _post_batch(self, documents: list[dict]) -> list[list[float]]:
        resp = requests.post(
            f"{self.base_url}/embed_documents",
            # Contract: the server's EmbedDocumentsRequest takes "items"
            # ({"title", "text", "is_code"}); "documents" is rejected 422.
            json={"items": documents, "task": "question_answering"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        vectors = body.get("vectors", [])
        if len(vectors) != len(documents):
            raise RuntimeError(
                f"embedding service returned {len(vectors)} vectors for "
                f"{len(documents)} chunks: refusing to misalign."
            )
        dim = body.get("dimension", EMBEDDING_DIMENSION)
        if dim != EMBEDDING_DIMENSION:
            raise RuntimeError(
                f"embedding dimension {dim} != {EMBEDDING_DIMENSION}: "
                "refusing to silently mix widths."
            )
        return vectors

    def embed_chunks(self, chunks: list[Chunk]) -> list[list[float]]:
        if not chunks:
            return []
        all_vectors: list[list[float]] = []
        for start in range(0, len(chunks), self.batch_size):
            batch_chunks = chunks[start : start + self.batch_size]
            documents = [
                {
                    "text": c.text,
                    "title": " > ".join(c.headings),
                    "is_code": bool(c.code_language),
                }
                for c in batch_chunks
            ]
            all_vectors.extend(self._post_batch(documents))
        return all_vectors


# ---------------------------------------------------------------------------
# vector store (Milvus sage_chunks)
# ---------------------------------------------------------------------------


class VectorStore(abc.ABC):
    """Where chunk records go. Milvus agent owns the real schema."""

    @abc.abstractmethod
    def upsert(self, records: list[dict]) -> int:
        """Insert chunk records; return count indexed."""

    @abc.abstractmethod
    def delete_version(self, library_id: str, version: str) -> int:
        """Delete all chunks for a (library, version); return count deleted."""

    @abc.abstractmethod
    def delete_chunks(self, chunk_ids: list[str]) -> int:
        """Delete chunk records by ID (spec §4 orphan prune after a
        re-crawl inserts a new revision); return count deleted."""

    def fetch_vectors(self, chunk_ids: list[str]) -> dict[str, list[float]]:
        """Optional hook to retrieve existing dense vectors by chunk_id
        for cross-version aliasing (§6). Defaults to empty dict."""
        return {}


class FakeVectorStore(VectorStore):
    """In-memory store for tests."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def upsert(self, records: list[dict]) -> int:
        by_id = {r["chunk_id"]: i for i, r in enumerate(self.records) if "chunk_id" in r}
        for rec in records:
            cid = rec.get("chunk_id")
            if cid is not None and cid in by_id:
                self.records[by_id[cid]] = rec
            else:
                if cid is not None:
                    by_id[cid] = len(self.records)
                self.records.append(rec)
        return len(records)

    def delete_version(self, library_id: str, version: str) -> int:
        before = len(self.records)
        self.records = [
            r for r in self.records
            if not (r["library_id"] == library_id and r["version_id"] == version)
        ]
        return before - len(self.records)

    def delete_chunks(self, chunk_ids: list[str]) -> int:
        ids = set(chunk_ids)
        before = len(self.records)
        self.records = [r for r in self.records if r["chunk_id"] not in ids]
        return before - len(self.records)

    def fetch_vectors(self, chunk_ids: list[str]) -> dict[str, list[float]]:
        wanted = set(chunk_ids)
        return {
            r["chunk_id"]: list(r["dense"])
            for r in self.records
            if r.get("chunk_id") in wanted and r.get("dense")
        }


class MilvusVectorStore(VectorStore):
    """Real sink: Milvus ``sage_chunks`` collection (lazy pymilvus import)."""

    def __init__(
        self,
        collection_name: str | None = None,
        uri: str | None = None,
        token: str | None = None,
    ) -> None:
        self.collection_name = collection_name or os.environ.get("MILVUS_COLLECTION") or "sage_chunks"
        self.uri = uri or os.environ.get("MILVUS_URI", "http://milvus:19530")
        self.token = token or os.environ.get("MILVUS_TOKEN", "")
        self._collection = None

    def _connect(self):
        if self._collection is not None:
            return self._collection
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise RuntimeError(
                "pymilvus is not installed: cannot write to Milvus. "
                "Install the pinned requirements or use the Docker image."
            ) from exc
        client = MilvusClient(uri=self.uri, token=self.token or None)
        if not client.has_collection(self.collection_name):
            raise RuntimeError(
                f"Milvus collection {self.collection_name!r} does not exist: "
                "the milvus_setup agent owns the schema. Refusing to "
                "auto-create with a guessed schema."
            )
        self._collection = client
        return client

    def upsert(
        self,
        records: list[dict],
        *,
        batch_size: int = DEFAULT_MILVUS_UPSERT_BATCH_SIZE,
    ) -> int:
        if not records:
            return 0
        client = self._connect()
        total = 0
        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            res = client.upsert(collection_name=self.collection_name, data=batch)
            count = res.get("upsert_count", res.get("insert_count"))
            if count is None or int(count) != len(batch):
                raise RuntimeError(
                    f"Milvus did not confirm every upsert: {count!r} of {len(batch)}"
                )
            total += int(count)
        return total

    def delete_version(self, library_id: str, version: str) -> int:
        client = self._connect()
        expr = (
            f'library_id == "{library_id}" and version_id == "{version}"'
        )
        res = client.delete(collection_name=self.collection_name, filter=expr)
        return res.get("delete_count", 0)

    def delete_chunks(self, chunk_ids: list[str], *, batch_size: int = 500) -> int:
        """Delete by chunk ID in batches (Milvus ``in`` filter)."""
        if not chunk_ids:
            return 0
        client = self._connect()
        total = 0
        for i in range(0, len(chunk_ids), batch_size):
            batch = chunk_ids[i : i + batch_size]
            lit = ",".join(
                '"' + c.replace("\\", "\\\\").replace('"', '\\"') + '"'
                for c in batch
            )
            res = client.delete(
                collection_name=self.collection_name,
                filter=f"chunk_id in [{lit}]",
            )
            total += res.get("delete_count", 0)
        return total

    def fetch_vectors(
        self, chunk_ids: list[str], *, batch_size: int = 500
    ) -> dict[str, list[float]]:
        if not chunk_ids:
            return {}
        client = self._connect()
        out: dict[str, list[float]] = {}
        for i in range(0, len(chunk_ids), batch_size):
            batch = chunk_ids[i : i + batch_size]
            lit = ",".join(
                '"' + c.replace("\\", "\\\\").replace('"', '\\"') + '"'
                for c in batch
            )
            rows = client.query(
                collection_name=self.collection_name,
                filter=f"chunk_id in [{lit}]",
                output_fields=["chunk_id", "dense"],
                limit=len(batch),
            )
            for row in rows or []:
                if row.get("chunk_id") and row.get("dense"):
                    out[row["chunk_id"]] = [float(x) for x in row["dense"]]
        return out


# ---------------------------------------------------------------------------
# catalog writer (sibling package seam)
# ---------------------------------------------------------------------------


class CatalogWriter(abc.ABC):
    """What the pipeline needs from the catalog (Postgres).

    The sibling catalog agent implements this; :func:`load_catalog_writer`
    resolves it. Tests inject a fake.
    """

    @abc.abstractmethod
    def page_hashes(self, library_id: str, version: str) -> set[str]:
        """Content hashes of pages already indexed for (library, version)."""

    @abc.abstractmethod
    def upsert_page(self, page: Page, *, chunk_count: int) -> None:
        """Write the page row (spec §4: page)."""

    @abc.abstractmethod
    def insert_chunks(self, chunks: list[Chunk]) -> int:
        """Write chunk metadata rows (spec §4: chunk)."""

    @abc.abstractmethod
    def alias_page(
        self, library_id: str, from_version: str, to_version: str, page: Page
    ) -> None:
        """Record an unchanged page aliased from a previous version (§6)."""

    @abc.abstractmethod
    def activate_version(self, library_id: str, version: str) -> None:
        """Mark exactly one active version per (library, version string)."""


class NullCatalogWriter(CatalogWriter):
    """Honest no-op: used when the sibling catalog is not yet wired.

    Test-only fake. Production ingestion never falls back to this writer.
    """

    def __init__(self) -> None:
        self.pages: list[Page] = []
        self.chunks: list[Chunk] = []
        self.aliased: list[tuple[str, str]] = []
        self.activated: list[tuple[str, str]] = []
        self._hashes: dict[tuple[str, str], set[str]] = {}

    def page_hashes(self, library_id: str, version: str) -> set[str]:
        return set(self._hashes.get((library_id, version), set()))

    def upsert_page(self, page: Page, *, chunk_count: int) -> None:
        self.pages.append(page)
        self._hashes.setdefault((page.library_id, page.version), set()).add(
            page.content_hash
        )

    def insert_chunks(self, chunks: list[Chunk]) -> int:
        self.chunks.extend(chunks)
        return len(chunks)

    def alias_page(self, library_id, from_version, to_version, page) -> None:
        self.aliased.append((from_version, to_version))
        self._hashes.setdefault((library_id, to_version), set()).add(
            page.content_hash
        )

    def activate_version(self, library_id: str, version: str) -> None:
        self.activated.append((library_id, version))


def load_catalog_writer(**kwargs) -> CatalogWriter:
    """Resolve the sibling catalog's writer.

    Contract for the catalog agent: ``services.catalog`` must expose
    ``make_catalog_writer() -> CatalogWriter`` (implemented in Wave 2 as
    ``services/catalog/writer.py``). Raises a clear error naming the
    contract when the sibling package is missing or ``DATABASE_URL`` is
    unset. Such failures propagate: no production success without a catalog.
    """
    try:
        from services.catalog import make_catalog_writer  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "sibling catalog package (services.catalog.make_catalog_writer) "
            "is not available yet. Pass an explicit catalog= writer, or wire "
            "the catalog agent's implementation."
        ) from exc
    return make_catalog_writer(**kwargs)


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------


def ingest_pages(
    library_id: str,
    version: str,
    pages: list[Page],
    source: str,
    *,
    prev_version: str | None = None,
    embedding_client: EmbeddingClient | None = None,
    vector_store: VectorStore | None = None,
    catalog: CatalogWriter | None = None,
    token_len=None,
    chunker_settings: ChunkerSettings | None = None,
) -> dict:
    """Ingest pages for one (library, version).

    chunk (doc_chunker) -> embed (embedding service) -> upsert to Milvus
    ``sage_chunks`` -> write page+chunk rows to catalog -> mark version
    active. Returns :class:`IngestReport` as a dict (picklable for RQ).

    Cross-version dedup (spec §6): when ``prev_version`` is given, pages
    whose ``content_hash`` matches the previous version are aliased and
    reuse the previous version's dense embeddings without re-calling the
    embedding model, while still indexing vectors and chunk rows under
    the new ``version`` so retrieval predicates (``version_id == version``)
    can find every unchanged page.
    """
    report = IngestReport(library_id=library_id, version=version)
    report.pages_total = len(pages)
    log.info(
        "ingest start: %s@%s — %d page(s), source=%s%s",
        library_id,
        version,
        len(pages),
        source,
        f", prev_version={prev_version}" if prev_version else "",
    )
    started = time.monotonic()
    if not pages:
        report.warnings.append("no pages supplied")
        return report.to_dict()

    vector_store = vector_store or MilvusVectorStore()
    if catalog is None:
        # Never report success with only Milvus vectors and no Postgres catalog.
        # Tests may explicitly inject a NullCatalogWriter; workers cannot.
        catalog = load_catalog_writer(default_source=source)

    # --- dedup against previous version (§6) ---------------------------
    prev_hashes: set[str] = set()
    if prev_version:
        try:
            prev_hashes = catalog.page_hashes(library_id, prev_version)
        except Exception as exc:  # fail open on dedup lookup, not on ingest
            log.warning("dedup lookup failed: %s", exc)
            report.warnings.append(f"dedup lookup failed: {exc}")

    chunker = DocChunker(settings=chunker_settings, token_len=token_len)
    aliased_chunks: list[Chunk] = []
    new_chunks: list[Chunk] = []
    for page in pages:
        if page.content_hash in prev_hashes:
            catalog.alias_page(library_id, prev_version, version, page)
            report.pages_aliased += 1
            aliased_page_chunks = chunker.chunk_page(
                library_id, version, page.url, page.content_hash, page.markdown
            )
            aliased_chunks.extend(aliased_page_chunks)
            continue
        chunks = chunker.chunk_page(
            library_id, version, page.url, page.content_hash, page.markdown
        )
        new_chunks.extend(chunks)
        catalog.upsert_page(page, chunk_count=len(chunks))
        report.pages_chunked += 1

    all_chunks = aliased_chunks + new_chunks
    if not all_chunks:
        catalog.activate_version(library_id, version)
        log.info(
            "ingested %s@%s: %d pages (all %d aliased), 0 chunks (%.2fs)",
            library_id,
            version,
            report.pages_total,
            report.pages_aliased,
            time.monotonic() - started,
        )
        return report.to_dict()

    # --- reuse vectors for aliased pages (§6) ----------------------------
    if aliased_chunks and prev_version:
        prev_id_map = {
            c.chunk_id: content_chunk_id(library_id, prev_version, c.page_id, c.text)
            for c in aliased_chunks
        }
        reused_from_store: dict[str, list[float]] = {}
        try:
            reused_from_store = vector_store.fetch_vectors(list(prev_id_map.values()))
        except Exception as exc:
            log.warning("vector reuse lookup from store failed: %s", exc)
        for chunk in aliased_chunks:
            prev_cid = prev_id_map[chunk.chunk_id]
            vec = reused_from_store.get(prev_cid)
            if vec is None:
                vec = _REUSE_CACHE.get(
                    library_id, chunk.page_id, chunk.chunk_order, chunk.text
                )
            if vec is not None:
                chunk.dense = vec
                _REUSE_CACHE.put(
                    library_id, chunk.page_id, chunk.chunk_order, chunk.text, vec
                )

    # NOTE: no local BM25 sparse vectors — the Milvus ``sage_chunks``
    # collection's server-side BM25 function (``text_to_bm25``) generates
    # the ``sparse`` field from the ``text`` field at insert time.
    # ``Chunk.to_milvus_record()`` already composes ``text`` as
    # chunk text + headings + code identifiers for that function.

    # --- embeddings (only chunks without a reused vector) ----------------
    chunks_to_embed = [c for c in all_chunks if not c.dense]
    if chunks_to_embed:
        client = embedding_client or HttpEmbeddingClient()
        dense_vectors = client.embed_chunks(chunks_to_embed)
        for chunk, vec in zip(chunks_to_embed, dense_vectors):
            chunk.dense = vec
            _REUSE_CACHE.put(
                library_id, chunk.page_id, chunk.chunk_order, chunk.text, vec
            )

    # --- index ------------------------------------------------------------
    records = [c.to_milvus_record() for c in all_chunks]
    report.chunks_indexed = vector_store.upsert(records)
    if report.chunks_indexed != len(records):
        raise RuntimeError(
            f"Milvus indexed {report.chunks_indexed} of {len(records)} chunks"
        )
    inserted = catalog.insert_chunks(all_chunks)
    if inserted != len(all_chunks):
        raise RuntimeError(
            f"catalog recorded {inserted} of {len(all_chunks)} chunks"
        )
    report.oversized_code_chunks = sum(1 for c in all_chunks if c.oversized_code)
    if report.oversized_code_chunks:
        report.warnings.append(
            f"{report.oversized_code_chunks} oversized code chunk(s) emitted whole "
            "(over the chunk budget, flagged oversized_code)"
        )

    # --- activate ---------------------------------------------------------
    catalog.activate_version(library_id, version)
    log.info(
        "ingested %s@%s: %d pages (%d aliased), %d chunks (%.2fs)",
        library_id, version, report.pages_total, report.pages_aliased,
        report.chunks_indexed,
        time.monotonic() - started,
    )
    return report.to_dict()
