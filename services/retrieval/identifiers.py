"""Identifier-like token extraction for the literal-match path (§9.1).

Two token sources feed the identifier branch:

1. :func:`extract_identifier_tokens` — identifier-shaped spans in the query
   (codes, references, serial-like runs), reused verbatim from new_DRUID.
2. :func:`library_name_mentions` — the resolved library's canonical name and
   aliases (from the catalog), matched case-insensitively against the topic.
   This branch IS the ``resolve-library-id`` semantic: a query naming the
   library boosts all its chunks (§9.1).
"""

from __future__ import annotations

import re

# Ordered by specificity; first match wins per span. Kept intentionally
# narrow — false positives here only cost a cheap extra branch query.
_PATTERNS: tuple[re.Pattern, ...] = (
    # UUIDs
    re.compile(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    ),
    # Clause / section references: "§ 4.2", "clause 7", "section 3.1.4"
    re.compile(r"§\s?\d+(?:\.\d+)*"),
    re.compile(r"\b(?:clause|section|article)\s+\d+(?:\.\d+)*\b", re.IGNORECASE),
    # Prefixed codes: "INV-2024-001", "PO 88123", "SN ABC123"
    re.compile(r"\b[A-Z]{2,}[-_ ]\d[\w-]*\b"),
    # Dashed numeric codes: "23441-HM8-B60"
    re.compile(r"\b\d[\w]*-\w+-\w+\b"),
    # Long digit runs that are unlikely to be prose: order/serial numbers
    re.compile(r"\b\d{6,}\b"),
)


def extract_identifier_tokens(query: str) -> list[str]:
    """Return deduplicated identifier-like tokens from ``query``.

    Returns [] when the query carries no identifier-like tokens — the
    caller then skips that part of the identifier branch.
    """
    found: list[str] = []
    seen: set[str] = set()
    for pattern in _PATTERNS:
        for match in pattern.finditer(query):
            token = match.group(0).strip()
            key = token.lower()
            if key not in seen:
                seen.add(key)
                found.append(token)
    return found


def library_name_mentions(
    topic: str, name: str, aliases: tuple[str, ...] = ()
) -> list[str]:
    """Return the library name/aliases mentioned verbatim in ``topic``.

    Case-insensitive substring match. When a shorter name is subsumed by a
    longer match already found (e.g. "react" inside "reactjs"), only the
    most specific match is returned.
    """
    lowered = (topic or "").lower()
    candidates = sorted({c for c in (name, *aliases) if c}, key=len, reverse=True)
    found: list[str] = []
    for candidate in candidates:
        key = candidate.lower()
        if key in lowered and not any(key in f.lower() for f in found):
            found.append(candidate)
    return found
