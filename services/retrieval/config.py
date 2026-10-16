"""Tunable retrieval parameters (spec §9).

All values are starting points to be tuned against the §16 eval corpus.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class RetrievalConfig:
    """Retrieval targets and fusion/rerank tuning knobs (§9)."""

    dense_candidates: int = 50
    lexical_candidates: int = 50
    identifier_candidates: int = 20
    overview_candidates: int = 50
    # Rerank width (§17 decision 5).
    rerank_candidates: int = 50
    # Hard cap on the requested top_k.
    max_top_k: int = 100
    # RRF damping constant: score = sum(1 / (k + rank)).
    rrf_k: int = 60
    # Fixed additive boost applied to identifier-match hits during fusion.
    identifier_boost: float = 1.0
    # Bounded additive cross-encoder bonus for exact API reference matches.
    code_symbol_bonus: float = 0.35
    # Expected embedding width; the query vector is validated against it.
    embedding_dimension: int = 768

    @classmethod
    def from_env(cls) -> "RetrievalConfig":
        return cls(
            dense_candidates=_int("RETRIEVAL_DENSE_CANDIDATES", 50),
            lexical_candidates=_int("RETRIEVAL_LEXICAL_CANDIDATES", 50),
            identifier_candidates=_int("RETRIEVAL_IDENTIFIER_CANDIDATES", 20),
            overview_candidates=_int("RETRIEVAL_OVERVIEW_CANDIDATES", 50),
            rerank_candidates=_int("RETRIEVAL_RERANK_CANDIDATES", 50),
            max_top_k=_int("RETRIEVAL_MAX_TOP_K", 100),
            rrf_k=_int("RETRIEVAL_RRF_K", 60),
            identifier_boost=_float("RETRIEVAL_IDENTIFIER_BOOST", 1.0),
            code_symbol_bonus=max(0.0, min(1.0, _float("RETRIEVAL_CODE_SYMBOL_BONUS", 0.35))),
            embedding_dimension=_int("EMBEDDING_DIMENSION", 768),
        )
