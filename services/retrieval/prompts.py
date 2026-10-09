"""Embedding task selection for queries (spec §8).

Unlike new_DRUID, the prompt strings themselves are NOT built here: the
embedding service applies the contractual PrefixContract prompt strings
server-side (byte-identical to new_DRUID's
``services/ingestion/embeddings.py`` — see README for the verbatim contract).
This module only selects the task string the client sends with the raw
query text.
"""

from __future__ import annotations

import re

TASK_QUESTION_ANSWERING = "question_answering"
TASK_CODE_RETRIEVAL = "code_retrieval"
TASK_SEARCH_RESULT = "search_result"

_VALID_TASKS = frozenset(
    {TASK_QUESTION_ANSWERING, TASK_CODE_RETRIEVAL, TASK_SEARCH_RESULT}
)

# Heuristic signals that a query is asking about code. Conservative on
# purpose: false "question answering" is the safe default, and callers may
# always pass an explicit task_hint.
_CODE_PATTERNS = (
    re.compile(r"`"),  # inline or fenced code
    # NOTE: `return` deliberately excluded — "return policy" is prose.
    re.compile(r"\b(def|class|function|fn|import|from|const|let|var)\s+\w+"),
    re.compile(r"=>"),
    re.compile(r"\w+\([^)]*\)\s*{"),
    re.compile(r";\s*$", re.MULTILINE),
)


def detect_task(query: str, task_hint: str | None = None) -> str:
    """Choose the embedding task: explicit hint wins, else a code heuristic."""
    if task_hint is not None:
        if task_hint not in _VALID_TASKS:
            raise ValueError(f"unknown task_hint {task_hint!r}")
        return task_hint
    if any(p.search(query) for p in _CODE_PATTERNS):
        return TASK_CODE_RETRIEVAL
    return TASK_QUESTION_ANSWERING


def validate_task(task: str) -> str:
    """Return ``task`` unchanged when valid, else raise ValueError."""
    if task not in _VALID_TASKS:
        raise ValueError(f"unknown embedding task {task!r}")
    return task
