"""Downstream service clients for the New_sage MCP server.

The MCP server never talks to Milvus, Postgres, or the queue directly.
Library resolution is served by :mod:`resolver` (P1 seed table; catalog
DB integration is the P2 seam). Retrieval is delegated over HTTP to the
retrieval service.

:class:`InMemoryRetrievalClient` is a fake for contract tests and local
development only — never production.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared value objects (produced by the owning services, consumed here)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetrievedPassage:
    """One retrieval hit, before MCP provenance wrapping."""

    text: str
    url: str
    section: list[str] = field(default_factory=list)
    code_language: str | None = None
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class RetrieveResult:
    """What the retrieval service reported for one get-library-docs call."""

    library: str
    version: str
    version_fallback: bool = False
    passages: list[RetrievedPassage] = field(default_factory=list)
    status: str | None = None  # e.g. "unknown version", "version stale"


class RetrievalError(Exception):
    """The retrieval service could not be reached or returned garbage."""


# ---------------------------------------------------------------------------
# Abstract client
# ---------------------------------------------------------------------------


class RetrievalServiceClient(ABC):
    """Version-scoped hybrid retrieval (§9), served over HTTP in prod."""

    @abstractmethod
    async def retrieve(
        self,
        *,
        library_id: str,
        version: str,
        topic: str | None,
        top_k: int,
        token_budget: int,
    ) -> RetrieveResult:
        """Return provenance-tagged passages for (library_id, version)."""


# ---------------------------------------------------------------------------
# Production client: HTTP to the retrieval service
# ---------------------------------------------------------------------------


class HttpRetrievalServiceClient(RetrievalServiceClient):
    """POSTs to ``{base_url}/retrieve``.

    Request:  {"library_id", "version", "topic", "top_k": 50,
               "token_budget": tokens}
    Response: {"library", "version", "version_fallback",
               "passages": [{"text", "url", "section", "code_language",
                             "score"}]}
    """

    def __init__(self, base_url: str, timeout_seconds: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def retrieve(
        self,
        *,
        library_id: str,
        version: str,
        topic: str | None,
        top_k: int,
        token_budget: int,
    ) -> RetrieveResult:
        payload = {
            "library_id": library_id,
            "version": version,
            "topic": topic,
            "top_k": top_k,
            "token_budget": token_budget,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/retrieve", json=payload
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error(
                "retrieval request to %s failed for %s@%s: %s",
                self._base_url,
                library_id,
                version,
                exc,
            )
            raise RetrievalError(
                f"retrieval service at {self._base_url} failed: {exc}"
            ) from exc

        try:
            passages = [
                RetrievedPassage(
                    text=p["text"],
                    url=p.get("url", ""),
                    section=list(p.get("section") or []),
                    code_language=p.get("code_language"),
                    score=float(p["score"]) if p.get("score") is not None else 0.0,
                )
                for p in data.get("passages", [])
            ]
        except (KeyError, TypeError, ValueError) as exc:
            log.error(
                "retrieval service at %s returned a malformed response for %s@%s: %s",
                self._base_url,
                library_id,
                version,
                exc,
            )
            raise RetrievalError(
                f"retrieval service returned a malformed response: {exc}"
            ) from exc

        return RetrieveResult(
            library=str((data["library"].get("name") or data["library"].get("library_id") or library_id) if isinstance(data.get("library"), dict) else data.get("library", library_id)),
            version=str(data.get("version", version)),
            version_fallback=bool(data.get("version_fallback", False)),
            passages=passages,
            status=data.get("status"),
        )


# ---------------------------------------------------------------------------
# In-memory fake (tests / local dev only — never production)
# ---------------------------------------------------------------------------


class InMemoryRetrievalClient(RetrievalServiceClient):
    """Canned responses keyed by (library_id, version).

    ``fallbacks`` maps library_id -> {"status": str} style dicts returned
    when the requested version is not indexed, exercising the
    version_fallback path without a live retrieval service.
    """

    def __init__(
        self,
        passages: list[RetrievedPassage] | None = None,
        *,
        version_fallback: bool = False,
        status: str | None = None,
    ) -> None:
        self._passages = list(passages or [])
        self._version_fallback = version_fallback
        self._status = status
        self.calls: list[dict] = []

    async def retrieve(
        self,
        *,
        library_id: str,
        version: str,
        topic: str | None,
        top_k: int,
        token_budget: int,
    ) -> RetrieveResult:
        self.calls.append(
            {
                "library_id": library_id,
                "version": version,
                "topic": topic,
                "top_k": top_k,
                "token_budget": token_budget,
            }
        )
        return RetrieveResult(
            library=library_id,
            version=version,
            version_fallback=self._version_fallback,
            passages=self._passages[:top_k],
            status=self._status,
        )
