"""Contract tests: the New_sage MCP compatibility tool surface (§10).

- Exactly 3 tools registered under their exact names.
- Every tool description instructs the caller to treat retrieved text as
  untrusted data, never instructions.
- Tool schemas expose no identity/auth parameters.
- Tool behavior against fake downstream clients: resolve contract,
  docs contract, version-fallback degradation, token budgeting,
  retrieval outage degradation, unknown library_id.
"""

from __future__ import annotations

import pytest

EXPECTED_TOOLS = ["resolve-library-id", "get-library-docs", "query-docs"]

# Parameter-name fragments that must never appear in any tool schema
# (no auth / identity / arbitrary-fetch surface on this server). Note:
# "tokens" (the Context7 token budget) is legitimate — the auth-token
# check below is exact-name, not substring.
FORBIDDEN_FRAGMENTS = (
    "tenant",
    "group_id",
    "user_id",
    "password",
    "secret",
    "filter",
    "where",
    "sql",
    "path",
    "filepath",
    "file_path",
    "directory",
    "fetch",
    "download",
)

# Exact parameter names that would indicate an auth surface.
FORBIDDEN_PARAM_NAMES = (
    "auth_token",
    "api_key",
    "authorization",
    "bearer",
    "jwt",
)


@pytest.mark.asyncio()
async def test_expected_three_tools_registered(fastmcp):
    tools = await fastmcp.list_tools()
    names = sorted(t.name for t in tools)
    assert names == sorted(EXPECTED_TOOLS), f"unexpected tool set: {names}"


@pytest.mark.asyncio()
async def test_tool_descriptions_instruct_data_not_instructions(fastmcp):
    tools = await fastmcp.list_tools()
    assert len(tools) == 3
    for tool in tools:
        desc = (tool.description or "").lower()
        assert "untrusted" in desc, f"{tool.name}: description must say text is untrusted"
        assert "never" in desc and "instruction" in desc, (
            f"{tool.name}: description must instruct callers never to treat "
            "text as instructions"
        )


@pytest.mark.asyncio()
async def test_no_forbidden_parameters_in_schemas(fastmcp):
    tools = await fastmcp.list_tools()
    for tool in tools:
        props = tool.inputSchema.get("properties", {})
        for param in props:
            lowered = param.lower()
            assert lowered not in FORBIDDEN_PARAM_NAMES, (
                f"{tool.name}: parameter '{param}' looks like an auth "
                "capability"
            )
            for frag in FORBIDDEN_FRAGMENTS:
                assert frag not in lowered, (
                    f"{tool.name}: parameter '{param}' looks like a forbidden "
                    f"capability (matched '{frag}')"
                )


@pytest.mark.asyncio()
async def test_expected_parameters_present(fastmcp):
    tools = {t.name: t for t in await fastmcp.list_tools()}
    assert set(tools["resolve-library-id"].inputSchema["properties"]) >= {
        "libraryName",
        "ecosystem",
    }
    assert set(tools["get-library-docs"].inputSchema["properties"]) >= {
        "library_id",
        "version",
        "topic",
        "tokens",
    }
    assert "libraryName" in tools["resolve-library-id"].inputSchema["required"]
    assert "library_id" in tools["get-library-docs"].inputSchema["required"]


# -- resolve-library-id behavior --------------------------------------------


async def test_resolve_tool_exact(deps):
    from mcp_server import tools as tool_impls
    from mcp_server.models import ResolveLibraryIdArgs

    out = await tool_impls.resolve_library_id(deps, ResolveLibraryIdArgs(libraryName="torch"))
    assert out["found"] is True
    assert out["library_id"] == "pytorch"
    assert out["latest"] == "2.8.0"
    assert out["versions"] == ["2.8.0", "2.7.1"]


async def test_resolve_tool_unknown_shape(deps):
    from mcp_server import tools as tool_impls
    from mcp_server.models import ResolveLibraryIdArgs

    out = await tool_impls.resolve_library_id(
        deps, ResolveLibraryIdArgs(libraryName="zzzqqqxxx")
    )
    assert out["found"] is False
    assert out["message"] == "unknown library"
    assert out["hint"] == "supply docs_url to register"


# -- get-library-docs behavior -----------------------------------------------


async def test_get_docs_defaults_to_latest(deps):
    from mcp_server import tools as tool_impls
    from mcp_server.models import GetLibraryDocsArgs

    out = await tool_impls.get_library_docs(deps, GetLibraryDocsArgs(library_id="react"))
    assert out["version"] == "19.2.0"
    assert out["version_fallback"] is False
    assert out["crawl_recommended"] is False
    assert "status" not in out
    call = deps.retrieval.calls[0]
    assert call["library_id"] == "react"
    assert call["version"] == "19.2.0"
    assert call["top_k"] == 50
    assert call["token_budget"] == 10000


async def test_get_docs_passes_topic_and_version(deps):
    from mcp_server import tools as tool_impls
    from mcp_server.models import GetLibraryDocsArgs

    out = await tool_impls.get_library_docs(
        deps,
        GetLibraryDocsArgs(library_id="react", version="18.3.1", topic="hooks", tokens=500),
    )
    assert out["version"] == "18.3.1"
    call = deps.retrieval.calls[0]
    assert call["version"] == "18.3.1"
    assert call["topic"] == "hooks"
    assert call["token_budget"] == 500


async def test_get_docs_unknown_library_id_graceful(deps):
    from mcp_server import tools as tool_impls
    from mcp_server.models import GetLibraryDocsArgs

    out = await tool_impls.get_library_docs(
        deps, GetLibraryDocsArgs(library_id="notalibrary")
    )
    assert out["passages"] == []
    assert out["crawl_recommended"] is True
    assert "unknown library_id" in out["status"]
    assert deps.retrieval.calls == []  # retrieval never consulted


async def test_get_docs_version_fallback_degrades(resolver):
    from mcp_server.clients import InMemoryRetrievalClient
    from mcp_server import tools as tool_impls
    from mcp_server.models import GetLibraryDocsArgs
    from mcp_server.tools import ServiceDeps

    from .conftest import sample_hit

    retrieval = InMemoryRetrievalClient(
        [sample_hit()],
        version_fallback=True,
        status="version '18.0.0' is not indexed; serving '19.2.0' instead",
    )
    local_deps = ServiceDeps(resolver=resolver, retrieval=retrieval)
    out = await tool_impls.get_library_docs(
        local_deps, GetLibraryDocsArgs(library_id="react", version="18.0.0")
    )
    assert out["version_fallback"] is True
    assert out["crawl_recommended"] is True
    assert "not indexed" in out["status"]
    assert len(out["passages"]) == 1  # still serves what the index has


async def test_get_docs_retrieval_outage_degrades(resolver):
    from mcp_server.clients import RetrievalError, RetrievalServiceClient
    from mcp_server import tools as tool_impls
    from mcp_server.models import GetLibraryDocsArgs
    from mcp_server.tools import ServiceDeps

    class DownClient(RetrievalServiceClient):
        async def retrieve(self, **kwargs):
            raise RetrievalError("connection refused")

    local_deps = ServiceDeps(resolver=resolver, retrieval=DownClient())
    out = await tool_impls.get_library_docs(
        local_deps, GetLibraryDocsArgs(library_id="react")
    )
    assert out["passages"] == []
    assert "retrieval service unavailable" in out["status"]
    assert out["version_fallback"] is False


async def test_get_docs_token_budget_truncates(resolver):
    from mcp_server.clients import InMemoryRetrievalClient
    from mcp_server import tools as tool_impls
    from mcp_server.models import GetLibraryDocsArgs
    from mcp_server.tools import ServiceDeps

    from .conftest import sample_hit

    hits = [sample_hit(text="x" * 400) for _ in range(5)]  # ~100 tokens each
    retrieval = InMemoryRetrievalClient(hits)
    local_deps = ServiceDeps(resolver=resolver, retrieval=retrieval)
    out = await tool_impls.get_library_docs(
        local_deps, GetLibraryDocsArgs(library_id="react", tokens=250)
    )
    assert len(out["passages"]) == 2  # 2 fit in 250 tokens
    assert "truncated" in out["status"]



async def test_http_retrieval_client_handles_overview_null_score_and_dict_library(monkeypatch):
    """P1 regression: RetrievalService.retrieve(topic=None).to_dict() emits
    score=None on passages and library={'library_id': ..., 'name': ..., 'ecosystem': ...}.
    HttpRetrievalServiceClient.retrieve must not crash with float(None) or stringify a dict."""
    import httpx
    from mcp_server.clients import HttpRetrievalServiceClient

    overview_payload = {
        "library": {"library_id": "react", "name": "React", "ecosystem": "npm"},
        "version": "19.2.0",
        "version_fallback": False,
        "passages": [
            {
                "text": "# Getting Started with React",
                "url": "https://react.dev/learn",
                "section": ["Getting Started"],
                "code_language": None,
                "score": None,
            }
        ],
    }

    class _FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json=None):
            request = httpx.Request("POST", url)
            return httpx.Response(200, json=overview_payload, request=request)

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    client = HttpRetrievalServiceClient("http://retrieval:8001")
    res = await client.retrieve(
        library_id="react", version="19.2.0", topic=None, top_k=5, token_budget=2000
    )
    assert res.library == "React"
    assert res.version == "19.2.0"
    assert len(res.passages) == 1
    assert res.passages[0].score == 0.0
