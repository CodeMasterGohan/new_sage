"""Retrieval service errors. All failures are fail-closed."""


class RetrievalError(Exception):
    """Base class for retrieval failures."""


class UnknownLibraryError(RetrievalError):
    """The requested library_id has no record in the catalog (HTTP 404)."""


class VersionResolutionError(RetrievalError):
    """No version could be resolved — not even an is_latest fallback."""


class DependencyError(RetrievalError):
    """An injected collaborator (embedding, vector store, catalog, reranker)
    failed or returned a contract-violating response."""
