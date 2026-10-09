"""New_sage ingestion service.

Pipeline: sources (repo clones, DevDocs docsets, tldr, crawl HTML, PDF
fallback) -> Page objects -> heading-based chunking -> EmbeddingGemma 2
(via the embedding service) -> Milvus ``sage_chunks`` -> catalog.

Production runs as an RQ worker (``worker.py``) consuming the
``sage-ingest`` queue; ``pipeline.ingest_pages`` is directly callable for
tests and local runs.
"""

from .models import Chunk, Page

__all__ = ["Page", "Chunk"]
