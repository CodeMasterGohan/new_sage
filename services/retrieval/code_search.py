"""Code-aware query matching with no schema change or model dependency.

Exact function/class/resource identifiers are more useful than generic semantic
similarity for reference documentation. This stage adds a bounded, explicit
relevance bonus after the cross-encoder, not an uncalibrated replacement.

Only high-signal symbols are extracted: quoted identifiers, dotted members,
call syntax, snake_case names, and mixed-case APIs. Ordinary prose is ignored.
"""
from __future__ import annotations

import re
from typing import Sequence

from .models import StoredChunk

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_BACKTICK = re.compile(r"`([A-Za-z_][A-Za-z0-9_.:]{1,127})`")
_DOTTED = re.compile(rf"\b({_IDENTIFIER}(?:\.{_IDENTIFIER})+)\b")
_CALL = re.compile(rf"\b({_IDENTIFIER})\s*\(")
_SNAKE = re.compile(r"\b([A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)\b")
_MIXED = re.compile(r"\b([A-Za-z][a-z0-9]*[A-Z][A-Za-z0-9]*)\b")
_STOP = frozenset(("if", "for", "while", "def", "class", "return", "async", "await",
                   "print", "len", "new", "list", "str", "map", "set",
                   "numpy", "pytorch", "fastapi", "typescript", "javascript",
                   "postgresql", "postgres", "django", "pandas", "react", "redis"))


def extract_code_symbols(query: str, limit: int = 8) -> list[str]:
    """Deduplicate high-signal API names while preserving spelling."""
    if limit <= 0:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for pattern in (_BACKTICK, _DOTTED, _CALL, _SNAKE, _MIXED):
        for match in pattern.finditer(query or ""):
            value = match.group(1)
            key = value.casefold()
            if key not in seen and key not in _STOP and len(value) <= 128:
                result.append(value)
                seen.add(key)
                if len(result) == limit:
                    return result
    return result


def _exact_span(text: str, symbol: str) -> bool:
    """Exact identifier boundaries: avoid 'useEffect' matching 'useEffects'."""
    return bool(re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_]|\.[A-Za-z_])",
        text, re.IGNORECASE,
    ))


def code_match_strength(symbols: Sequence[str], chunk: StoredChunk) -> float:
    """0..1 quality of symbol evidence, highest for a reference heading.

    Uses existing heading and text fields. Definition syntax and headings
    are stronger evidence than an incidental prose mention. Avoid inferring
    source semantics from embedding similarity.
    """
    if not symbols:
        return 0.0
    heading = " / ".join(chunk.headings)
    text = chunk.text
    best = 0.0
    for symbol in symbols:
        if _exact_span(heading, symbol):
            return 1.0
        name = symbol.rsplit(".", 1)[-1]
        definition = re.compile(
            rf"(?m)^\s*(?:async\s+)?(?:def|class|function|func|fn)\s+"
            rf"{re.escape(name)}\b|\b{re.escape(name)}\s*\([^\n)]*\)\s*(?:->|:|\{{)",
            re.IGNORECASE,
        )
        if definition.search(text):
            best = max(best, 0.85)
        elif _exact_span(text, symbol):
            best = max(best, 0.35)
    return best
