"""Standalone stdio adapter for local use.

This is a SEPARATE transport from the Streamable HTTP service. There is no
authentication on either transport (spec §13: single user, own network), so
there is no trusted credential context to configure — just point the
adapter at the retrieval service (or use the in-memory fakes for local
development).

The stdio adapter and the HTTP service must never share a process.

Usage:
    python -m mcp_server.stdio_adapter \\
        --retrieval-url http://localhost:8001 \\
        --resolver-table /path/to/libraries_seed.json
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .clients import HttpRetrievalServiceClient, InMemoryRetrievalClient
from .resolver import LibraryResolver
from .server import build_fastmcp_for_tests
from .tools import ServiceDeps

# IMPORTANT: this adapter speaks MCP over STDOUT. All logging goes to
# stderr (enforced in main()) so stdout stays protocol-clean.
log = logging.getLogger(__name__)

_DEFAULT_SEED = Path(__file__).parent / "libraries_seed.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="new_sage MCP stdio adapter (no auth)")
    p.add_argument(
        "--retrieval-url",
        default=os.getenv("RETRIEVAL_URL", "http://retrieval:8001"),
        help="Base URL of the retrieval service.",
    )
    p.add_argument(
        "--resolver-table",
        default=os.getenv("SAGE_RESOLVER_TABLE", str(_DEFAULT_SEED)),
        help="Path to the resolver seed table JSON.",
    )
    p.add_argument(
        "--dev-fake-clients",
        action="store_true",
        default=os.getenv("SAGE_MCP_DEV_FAKE_CLIENTS") == "1",
        help="Use the in-memory fake retrieval client (local dev only).",
    )
    return p.parse_args(argv)


async def _amain(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    log.info(
        "new_sage MCP stdio adapter starting: retrieval_url=%s resolver_table=%s "
        "dev_fake_clients=%s (stdio transport; no auth — spec §13)",
        args.retrieval_url,
        args.resolver_table,
        args.dev_fake_clients,
    )
    try:
        resolver = LibraryResolver.from_seed_file(args.resolver_table)
    except OSError as exc:
        log.error("resolver table unreadable: %s", exc)
        raise
    if args.dev_fake_clients:
        log.warning("using in-memory fake retrieval client (local dev only)")
        retrieval = InMemoryRetrievalClient()
    else:
        retrieval = HttpRetrievalServiceClient(args.retrieval_url)
    deps = ServiceDeps(resolver=resolver, retrieval=retrieval)
    mcp = build_fastmcp_for_tests(deps)
    log.info("new_sage MCP stdio adapter ready (logging on stderr)")
    try:
        await mcp.run_stdio_async()
    except Exception:
        log.exception("stdio adapter crashed")
        raise
    log.info("new_sage MCP stdio adapter stopped")


def main() -> None:
    # stderr ONLY: stdout carries the MCP JSON-RPC stream.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
