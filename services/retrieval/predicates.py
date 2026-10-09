"""Version predicate: the (library_id, version_id) scoping rule (§9.2).

Every query carries ONE resolved predicate, applied IDENTICALLY to the
dense, BM25, and identifier branches (and overview mode). This replaces
new_DRUID's tenant predicate — there is exactly one user, no tenancy.
"""

from __future__ import annotations

from dataclasses import dataclass


def _escape(value: str) -> str:
    """Escape a string literal for a Milvus filter expression."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True)
class VersionPredicate:
    """Immutable (library_id, version_id) scoping for all retrieval branches."""

    library_id: str
    version_id: str

    def to_milvus_expr(self) -> str:
        """Render as a Milvus filter expression over ``sage_chunks``."""
        return (
            f"library_id == {_escape(self.library_id)}"
            f" and version_id == {_escape(self.version_id)}"
        )
