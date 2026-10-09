"""Shared fixtures for the New_sage MCP server contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.clients import InMemoryRetrievalClient, RetrievedPassage
from mcp_server.models import PassageProvenance
from mcp_server.resolver import LibraryResolver
from mcp_server.server import build_fastmcp_for_tests
from mcp_server.tools import ServiceDeps

SEED = Path(__file__).resolve().parent.parent / "src" / "mcp_server" / "libraries_seed.json"


def sample_hit(**overrides) -> RetrievedPassage:
    base = {
        "text": "useEffect lets you synchronize a component with an external system.",
        "url": "https://react.dev/reference/react/useEffect",
        "section": ["API Reference", "Hooks", "useEffect"],
        "code_language": None,
        "score": 0.9,
    }
    base.update(overrides)
    return RetrievedPassage(**base)


@pytest.fixture()
def resolver() -> LibraryResolver:
    return LibraryResolver.from_seed_file(SEED)


@pytest.fixture()
def deps(resolver) -> ServiceDeps:
    return ServiceDeps(
        resolver=resolver,
        retrieval=InMemoryRetrievalClient([sample_hit()]),
    )


@pytest.fixture()
def fastmcp(deps):
    return build_fastmcp_for_tests(deps)


def sample_provenance(**overrides) -> PassageProvenance:
    base = {
        "library": "React",
        "version": "19.2.0",
        "is_latest": True,
        "url": "https://react.dev/reference/react/useEffect",
        "section": ["API Reference", "Hooks", "useEffect"],
        "code_language": None,
        "content_hash": None,
    }
    base.update(overrides)
    return PassageProvenance(**base)
