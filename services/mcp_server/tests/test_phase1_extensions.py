"""Phase 1 catalog, version-policy, and MCP compatibility regressions."""
import pytest

from mcp_server.models import GetLibraryDocsArgs
from mcp_server.clients import InMemoryRetrievalClient, RetrievedPassage
from mcp_server.catalog_resolver import CatalogRefreshingResolver
from mcp_server.tools import ServiceDeps, get_library_docs


def test_context7_ids_require_registered_repositories(resolver):
    record, version = resolver.get_by_context7_id("/facebook/react/18.3.1")
    assert record.library_id == "react" and version == "18.3.1"
    record, version = resolver.get_by_context7_id("/facebook/react@19.2.0")
    assert record.library_id == "react" and version == "19.2.0"
    assert resolver.get_by_context7_id("/unknown/untrusted") == (None, None)


@pytest.mark.asyncio
async def test_strict_version_refuses_wrong_version_and_enqueues(resolver):
    retriever = InMemoryRetrievalClient(
        [RetrievedPassage(text="wrong version", url="https://react.dev")],
        version_fallback=True,
    )
    enqueued = []
    deps = ServiceDeps(
        resolver=resolver, retrieval=retriever,
        crawl_dispatcher=lambda lib, ver: enqueued.append((lib, ver)) or "job123",
    )
    result = await get_library_docs(
        deps, GetLibraryDocsArgs(
            library_id="react", version="17.0.0", strict_version=True
        )
    )
    assert result["passages"] == []
    assert result["crawl_recommended"] is True
    assert enqueued == [("react", "17.0.0")]


@pytest.mark.asyncio
async def test_default_version_remains_backwards_compatible(resolver):
    retriever = InMemoryRetrievalClient(
        [RetrievedPassage(text="available documentation", url="https://react.dev")],
        version_fallback=True,
    )
    deps = ServiceDeps(resolver=resolver, retrieval=retriever)
    result = await get_library_docs(
        deps, GetLibraryDocsArgs(library_id="react", version="17.0.0")
    )
    assert len(result["passages"]) == 1
    assert result["version_fallback"] is True


def test_catalog_resolver_refresh_is_lazy(monkeypatch):
    from mcp_server import catalog_resolver
    calls = []

    class FakeResolver:
        def get(self, library_id):
            return library_id
        def resolve(self, library_name, ecosystem=None):
            return library_name
        def get_by_context7_id(self, library_id):
            return library_id, None

    monkeypatch.setattr(
        catalog_resolver.LibraryResolver, "from_catalog",
        lambda url: calls.append(url) or FakeResolver()
    )
    resolver = CatalogRefreshingResolver("postgresql://fake", ttl_seconds=60)
    assert resolver.get("react") == "react"
    assert resolver.get("vue") == "vue"
    assert calls == ["postgresql://fake"]


@pytest.mark.asyncio
async def test_query_docs_tool_has_expected_schema(fastmcp):
    tools = {t.name: t for t in await fastmcp.list_tools()}
    assert set(tools["query-docs"].inputSchema["properties"]) >= {"libraryId", "query"}
    assert {"libraryId", "query"} <= set(tools["query-docs"].inputSchema["required"])
