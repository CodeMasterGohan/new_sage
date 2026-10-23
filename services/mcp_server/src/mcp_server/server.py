"""MCP server assembly: Streamable HTTP transport, NO authentication.

Spec §13: New_sage serves one user on their own network. There is no JWT
validation, no middleware identity, no tenant concept — the auth layer from
the new_DRUID mcp_server was deleted wholesale, not disabled.

The only transport hygiene kept is Origin validation for browser-based
clients (DNS-rebinding protection per the MCP transport specification).
Absent Origin (native clients) is permitted.

Binding to localhost / the Tailscale interface is a deploy concern
(compose config), not code.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

log = logging.getLogger(__name__)

from .clients import (
    HttpRetrievalServiceClient,
    InMemoryRetrievalClient,
    RetrievedPassage,
)
from .resolver import LibraryResolver
from .catalog_resolver import CatalogRefreshingResolver
from .crawl_queue import dispatch_crawl
from . import tools as tool_impls
from .models import GetLibraryDocsArgs, ResolveLibraryIdArgs

_DEFAULT_SEED = Path(__file__).parent / "libraries_seed.json"


@dataclass
class Settings:
    retrieval_url: str = "http://retrieval:8001"
    resolver_table: str = str(_DEFAULT_SEED)
    allowed_origins: tuple[str, ...] = ()
    dev_fake_clients: bool = False
    database_url: str = ""
    resolver_refresh_seconds: int = 60
    auto_crawl: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        origins = tuple(
            o.strip()
            for o in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",")
            if o.strip()
        )
        return cls(
            retrieval_url=os.getenv("RETRIEVAL_URL", "http://retrieval:8001"),
            resolver_table=os.getenv("SAGE_RESOLVER_TABLE", str(_DEFAULT_SEED)),
            allowed_origins=origins,
            dev_fake_clients=os.getenv("SAGE_MCP_DEV_FAKE_CLIENTS") == "1",
            database_url=os.getenv("DATABASE_URL", ""),
            resolver_refresh_seconds=int(os.getenv("SAGE_RESOLVER_REFRESH_SECONDS", "60")),
            auto_crawl=os.getenv("SAGE_AUTO_CRAWL", "0") == "1",
        )


class OriginMiddleware(BaseHTTPMiddleware):
    """Reject requests from browser origins not on the allowlist.

    This is DNS-rebinding protection for browser-based MCP clients, not
    authentication — there is deliberately no auth on this server (§13).
    """

    def __init__(self, app, allowed_origins: tuple[str, ...]):
        super().__init__(app)
        self._allowed_origins = allowed_origins

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        origin = request.headers.get("origin")
        if origin and origin not in self._allowed_origins:
            log.warning(
                "rejecting request from disallowed origin %r (%s %s)",
                origin,
                request.method,
                request.url.path,
            )
            return JSONResponse({"error": "origin not allowed"}, status_code=403)
        return await call_next(request)


def _register_tools(mcp: FastMCP, deps: tool_impls.ServiceDeps) -> None:
    """Register the 2 tools, binding each to the shared service deps.

    The inner wrappers carry the implementation docstrings so the MCP tool
    descriptions include the data-not-instructions guidance.
    """

    def _doc(impl):
        def fix(wrapper):
            wrapper.__doc__ = impl.__doc__
            return wrapper

        return fix

    @mcp.tool(name="resolve-library-id")
    @_doc(tool_impls.resolve_library_id)
    async def resolve_library_id(
        libraryName: str = Field(
            min_length=1,
            max_length=200,
            description="Library name to resolve (e.g. 'react', 'pandas', 'torch').",
        ),
        ecosystem: str | None = Field(
            default=None,
            max_length=64,
            description="Optional ecosystem hint for disambiguation (e.g. 'pypi', 'npm', 'crates').",
        ),
        query: str | None = Field(
            default=None, max_length=2000,
            description="Optional task context for Context7-style clients.",
        ),
    ) -> dict:
        return await tool_impls.resolve_library_id(
            deps, ResolveLibraryIdArgs(libraryName=libraryName, ecosystem=ecosystem, query=query)
        )

    @mcp.tool(name="get-library-docs")
    @_doc(tool_impls.get_library_docs)
    async def get_library_docs(
        library_id: str = Field(
            description="Library ID from resolve-library-id (e.g. 'react')."
        ),
        version: str | None = Field(
            default=None,
            max_length=64,
            description="Version string to pin (e.g. '18.3.1'). Omit for latest.",
        ),
        topic: str | None = Field(
            default=None,
            max_length=2000,
            description="Free-text topic to scope retrieval. Omit for overview pages.",
        ),
        strict_version: bool = Field(
            default=False,
            description="Do not serve documentation from a different version.",
        ),
        tokens: int = Field(
            default=10000,
            ge=1,
            le=100000,
            description="Token budget cap for returned passage text.",
        ),
    ) -> dict:
        return await tool_impls.get_library_docs(
            deps,
            GetLibraryDocsArgs(
                library_id=library_id, version=version, topic=topic,
                tokens=tokens, strict_version=strict_version
            ),
        )


    @mcp.tool(name="query-docs")
    @_doc(tool_impls.get_library_docs)
    async def query_docs(
        libraryId: str = Field(
            min_length=1, max_length=300,
            description="Known /owner/repo[/version] or canonical library ID.",
        ),
        query: str = Field(
            min_length=1, max_length=2000,
            description="Specific documentation question.",
        ),
    ) -> dict:
        record, version = deps.resolver.get_by_context7_id(libraryId)
        if record is None:
            return {
                "library": libraryId,
                "passages": [],
                "crawl_recommended": False,
                "status": "Unknown library ID; call resolve-library-id first.",
            }
        return await tool_impls.get_library_docs(
            deps, GetLibraryDocsArgs(
                library_id=record.library_id, version=version, topic=query
            ),
        )


def _default_deps(settings: Settings) -> tool_impls.ServiceDeps:
    resolver = (
        CatalogRefreshingResolver(settings.database_url, settings.resolver_refresh_seconds)
        if settings.database_url
        else LibraryResolver.from_seed_file(settings.resolver_table)
    )
    if settings.dev_fake_clients:
        retrieval: tool_impls.RetrievalServiceClient = InMemoryRetrievalClient(
            [
                RetrievedPassage(
                    text="Dev-mode placeholder passage.",
                    url="https://example.invalid/dev",
                    section=["dev"],
                )
            ]
        )
    else:
        retrieval = HttpRetrievalServiceClient(settings.retrieval_url)
    return tool_impls.ServiceDeps(
        resolver=resolver,
        retrieval=retrieval,
        crawl_dispatcher=dispatch_crawl if settings.auto_crawl else None,
    )


def create_app(
    settings: Settings | None = None,
    deps: tool_impls.ServiceDeps | None = None,
) -> Starlette:
    """Build the Streamable HTTP MCP application (no auth)."""
    settings = settings or Settings.from_env()
    deps = deps or _default_deps(settings)

    mcp = FastMCP("new-sage")
    _register_tools(mcp, deps)

    # FastMCP owns both the /mcp route and its session-manager lifespan.
    app = mcp.streamable_http_app()
    app.add_middleware(OriginMiddleware, allowed_origins=settings.allowed_origins)
    log.info(
        "new_sage MCP server starting: retrieval_url=%s resolver_table=%s "
        "allowed_origins=%d dev_fake_clients=%s (no auth — spec §13)",
        settings.retrieval_url,
        settings.resolver_table,
        len(settings.allowed_origins),
        settings.dev_fake_clients,
    )
    return app


def build_fastmcp_for_tests(deps: tool_impls.ServiceDeps) -> FastMCP:
    """Test helper: the bare FastMCP server without HTTP middleware."""
    mcp = FastMCP("new-sage-test")
    _register_tools(mcp, deps)
    return mcp


# Convenience for `uvicorn --factory mcp_server.server:create_app`.
__all__ = ["create_app", "build_fastmcp_for_tests", "Settings", "OriginMiddleware"]
