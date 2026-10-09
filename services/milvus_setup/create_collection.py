"""One-shot Milvus provisioning for New_sage — spec §9.1.

Idempotent: safe to re-run. Creates the ``sage_chunks`` collection with the
§9.1 schema, the BM25 function, HNSW + SPARSE_INVERTED_INDEX indexes, and
loads it. No aliases, no partition keys (single user, spec §13).

Usage::

    python -m create_collection              # create-if-missing, verify schema
    python -m create_collection --check-only  # verify, change nothing
    python -m create_collection --recreate --yes  # DROP and rebuild (data loss!)

Environment:
    MILVUS_URI (default http://localhost:19531),
    MILVUS_COLLECTION (default sage_chunks),
    MILVUS_WAIT_TIMEOUT_S (default 300)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass

from .schema import (
    BM25_FUNCTION_NAME,
    COLLECTION_NAME,
    DENSE_DIM,
    build_index_params,
    build_pymilvus_schema,
    field_names,
)

log = logging.getLogger("milvus_setup")


class SetupError(RuntimeError):
    """Fatal provisioning failure — the one-shot job must exit non-zero."""


class SchemaMismatchError(SetupError):
    """An existing collection does not match the §9.1 schema.

    Never auto-migrated: rebuild with --recreate (dropping the corpus) or
    cut over via a new collection name + MILVUS_COLLECTION.
    """


@dataclass
class Settings:
    uri: str = "http://localhost:19531"
    collection: str = COLLECTION_NAME
    wait_timeout_s: float = 300.0

    @classmethod
    def from_env(cls) -> "Settings":
        import os

        return cls(
            uri=os.environ.get("MILVUS_URI", "http://localhost:19531"),
            collection=os.environ.get("MILVUS_COLLECTION", COLLECTION_NAME),
            wait_timeout_s=float(os.environ.get("MILVUS_WAIT_TIMEOUT_S", "300")),
        )


def connect_with_retry(uri: str, timeout_s: float):
    """Connect to Milvus, retrying until ``timeout_s`` (compose has no
    health-gated depends_on for this job, so the retry lives here)."""
    from pymilvus import MilvusClient

    deadline = time.monotonic() + timeout_s
    attempt = 0
    while True:
        attempt += 1
        try:
            client = MilvusClient(uri=uri)
            # A cheap round-trip proves the server is actually up.
            client.list_collections()
            log.info("connected to Milvus at %s (attempt %d)", uri, attempt)
            return client
        except Exception as exc:  # noqa: BLE001 — retry on any connect failure
            if time.monotonic() >= deadline:
                raise SetupError(
                    f"could not reach Milvus at {uri} within {timeout_s}s: {exc}"
                ) from exc
            wait = min(2.0 * attempt, 15.0)
            log.info("Milvus not ready (attempt %d): %s — retrying in %.1fs",
                     attempt, exc, wait)
            time.sleep(wait)


def verify_schema_compatible(client, collection: str) -> None:
    """Fail closed if an existing collection does not match §9.1.

    Checks: field-name set, dense dim, and the BM25 function wiring.
    Raises SchemaMismatchError on any drift.
    """
    desc = client.describe_collection(collection)
    fields = {f["name"]: f for f in desc["fields"]}
    missing = [n for n in field_names() if n not in fields]
    if missing:
        raise SchemaMismatchError(
            f"collection {collection!r} is missing fields {missing}; "
            "rebuild with --recreate or cut over via MILVUS_COLLECTION, never in place"
        )
    dim = None
    for f in desc["fields"]:
        if f["name"] == "dense":
            dim = f.get("params", {}).get("dim")
    if dim is not None and int(dim) != DENSE_DIM:
        raise SchemaMismatchError(
            f"collection {collection!r} dense dim={dim}, expected "
            f"{DENSE_DIM}; rebuild with --recreate or cut over via MILVUS_COLLECTION"
        )
    functions = {fn.get("name"): fn for fn in desc.get("functions", [])}
    if BM25_FUNCTION_NAME not in functions:
        raise SchemaMismatchError(
            f"collection {collection!r} lacks the {BM25_FUNCTION_NAME!r} BM25 "
            "function; rebuild with --recreate or cut over via MILVUS_COLLECTION"
        )
    log.info("existing collection %r matches the §9.1 schema", collection)


def ensure_indexes(client, collection: str) -> None:
    """Create any missing indexes (dense HNSW + sparse BM25)."""
    from pymilvus.milvus_client.index import IndexParams

    existing = {idx["field_name"] for idx in client.list_indexes(collection)}
    missing = [s for s in build_index_params() if s["field_name"] not in existing]
    if not missing:
        log.info("all indexes already exist on %r — skipping", collection)
        return
    index_params = IndexParams()
    for spec in missing:
        log.info("creating index on %r (%s/%s)", spec["field_name"],
                 spec["index_type"], spec["metric_type"])
        index_params.add_index(
            field_name=spec["field_name"],
            index_type=spec["index_type"],
            metric_type=spec["metric_type"],
            params=spec.get("params", {}),
        )
    client.create_index(collection, index_params)


def ensure_loaded(client, collection: str) -> None:
    state = client.get_load_state(collection_name=collection).get("state")
    if str(state).endswith("Loaded"):
        log.info("collection %r already loaded", collection)
        return
    log.info("loading collection %r", collection)
    client.load_collection(collection_name=collection)


def ensure_collection(settings: Settings, recreate: bool = False) -> None:
    """Full idempotent provisioning run."""
    client = connect_with_retry(settings.uri, settings.wait_timeout_s)
    try:
        if client.has_collection(settings.collection):
            if recreate:
                log.warning("dropping collection %r (--recreate)", settings.collection)
                client.drop_collection(settings.collection)
            else:
                log.info("collection %r exists — verifying", settings.collection)
                verify_schema_compatible(client, settings.collection)
        if not client.has_collection(settings.collection):
            log.info("creating collection %r", settings.collection)
            client.create_collection(
                collection_name=settings.collection,
                schema=build_pymilvus_schema(),
                index_params=build_index_params(),
            )
        ensure_indexes(client, settings.collection)
        ensure_loaded(client, settings.collection)
        log.info("Milvus provisioning complete: %r", settings.collection)
    finally:
        client.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check-only", action="store_true",
                        help="verify schema/indexes, change nothing")
    parser.add_argument("--recreate", action="store_true",
                        help="DROP the collection and rebuild (DATA LOSS)")
    parser.add_argument("--yes", action="store_true",
                        help="confirm --recreate")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    settings = Settings.from_env()

    try:
        if args.check_only:
            client = connect_with_retry(settings.uri, settings.wait_timeout_s)
            try:
                if not client.has_collection(settings.collection):
                    raise SetupError(f"collection {settings.collection!r} missing")
                verify_schema_compatible(client, settings.collection)
                log.info("check-only: OK")
            finally:
                client.close()
            return 0

        if args.recreate and not args.yes:
            log.error("--recreate requires --yes (it destroys data)")
            return 2
        ensure_collection(settings, recreate=args.recreate)
        return 0
    except SetupError as exc:
        log.error("provisioning failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
