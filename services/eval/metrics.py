"""Eval metrics for new_sage (spec §16).

All pure functions — no services, no models, no I/O — so they run
identically on the VM now and on the H200 later.
"""

from __future__ import annotations

from typing import Sequence


def recall_at_k(ranked_ids: Sequence[str], gold_id: str, k: int) -> float:
    """1.0 when the gold chunk id appears in the top-k ranked ids, else 0.0.

    ``ranked_ids`` is the retrieval system's final ordering (after fusion,
    rerank, and token-budget truncation); ``gold_id`` is the chunk id of the
    chunk containing the gold passage.
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    return 1.0 if gold_id in list(ranked_ids)[:k] else 0.0


def version_correctness(retrieved_version: str | None, asked_version: str) -> bool:
    """Did the system serve the version the question asked for?

    True only on an exact version-string match. A fallback to ``is_latest``
    when the asked version was unresolvable counts as incorrect — the
    provenance flag exists precisely so the caller can see the fallback
    happened, but the eval treats it as a miss.
    """
    return retrieved_version == asked_version


def resolver_accuracy(cases: Sequence[dict], resolver) -> dict:
    """Score a LibraryResolver over ``{query, ecosystem?, expected_library_id?}``.

    A case is correct when the resolver returns ``found=True`` with the
    expected ``library_id``, or returns ``found=False`` when the expected
    value is null/None (unknown library). Returns a summary dict with the
    accuracy and the list of failures for inspection.
    """
    failures: list[dict] = []
    for case in cases:
        query = case["query"]
        ecosystem = case.get("ecosystem")
        expected = case.get("expected_library_id")
        result = resolver.resolve(query, ecosystem=ecosystem)
        if expected:
            ok = bool(result.found) and result.library_id == expected
        else:
            ok = not result.found
        if not ok:
            failures.append(
                {
                    "query": query,
                    "ecosystem": ecosystem,
                    "expected": expected,
                    "got_found": bool(result.found),
                    "got_library_id": result.library_id,
                }
            )
    total = len(cases)
    return {
        "total": total,
        "correct": total - len(failures),
        "accuracy": (total - len(failures)) / total if total else 0.0,
        "failures": failures,
    }


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean (0.0 on empty input)."""
    values = list(values)
    return sum(values) / len(values) if values else 0.0
