"""Identifier extraction for docs chunks (spec §9: identifier channel).

BM25 is lexical relevance, not literal matching. This module extracts
exact-match code identifiers — function/class names from code blocks and
API references — so the retrieval layer's identifier branch can boost
exact-name lookups (e.g. "connect()" should never lose to a prose
mention of "connection").

Adapted from new_DRUID's ``identifiers.py``: document-identifier patterns
(part numbers, invoices) are replaced by code-identifier patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Heuristics, ordered by precision. Word-boundary anchored to avoid noise.
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{1,63})\s*\(")
_DEF_RE = re.compile(
    r"^\s*(?:def|class|function|fn|func|public|private|protected|static|async\s+def)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)
_CONST_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,63})\b")
_METHOD_RE = re.compile(r"(?:\\.|::|#)([A-Za-z_][A-Za-z0-9_]{1,63})")
_BACKTICK_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.:]{1,127})`")
_SNAKE_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)\b")

# Noise words that look like calls but carry no signal.
_STOPWORDS = frozenset(
    {
        "if", "for", "while", "switch", "catch", "return", "print", "len",
        "range", "int", "str", "list", "dict", "set", "tuple", "new",
        "typeof", "instanceof", "await", "import", "from", "with", "as",
    }
)


@dataclass
class CodeIdentifierExtractor:
    """Extract code identifiers from chunk text."""

    max_identifiers_per_chunk: int = 32

    def extract(self, text: str) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()

        def add(value: str) -> None:
            key = value.lower()
            if key in _STOPWORDS or key in seen:
                return
            seen.add(key)
            found.append(value)

        for m in _BACKTICK_RE.finditer(text):
            add(m.group(1))
        for m in _SNAKE_RE.finditer(text):
            add(m.group(1))
        for m in _DEF_RE.finditer(text):
            add(m.group(1))
        for m in _METHOD_RE.finditer(text):
            add(m.group(1))
        for m in _CALL_RE.finditer(text):
            add(m.group(1))
        for m in _CONST_RE.finditer(text):
            add(m.group(1))
        return found[: self.max_identifiers_per_chunk]


_extractor = CodeIdentifierExtractor()


def extract_code_identifiers(text: str) -> list[str]:
    """Convenience wrapper over the shared extractor."""
    return _extractor.extract(text)
