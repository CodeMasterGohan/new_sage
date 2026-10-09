"""New_sage crawler service (spec §5.2, §6, §9.3.1, §12).

On-demand versioned-docs crawler: polite fetching, version detection,
repo-clone version pinning, RustFS raw snapshots, and RQ jobs
(``sage-crawl`` queue) that feed the ingestion pipeline.

Import layout mirrors the sibling services: ``from services.crawler.jobs
import crawl_library_version``. The Docker image is built from
``~/workspace/new_sage/build`` so ``services.catalog`` and
``services.ingestion`` are importable siblings.
"""
