"""CPU rerank of retrieved candidates (§9.1).

All new_sage chunks are text — there is no page-image modality to bypass
here. The concrete :class:`MiniLMReranker` loads the cross-encoder from
pre-staged paths and FAILS CLOSED when the artifact is missing
(``HF_HUB_OFFLINE=1`` / ``TRANSFORMERS_OFFLINE=1`` are enforced so nothing
can download at runtime).
"""

from __future__ import annotations

import os
from typing import Sequence

from .interfaces import Reranker
from .models import FusedCandidate

# Conservative chars-per-token estimate for the budget guard. Production
# should enforce the budget with the MiniLM tokenizer; this guard only
# prevents pathological inputs from breaking the cross-encoder call.
_CHARS_PER_TOKEN = 4
MINILM_MAX_INPUT_TOKENS = 512
# Reserve room for the query + separators; chunking already targets 420.
MAX_PASSAGE_CHARS = 420 * _CHARS_PER_TOKEN


def truncate_to_budget(text: str, max_chars: int = MAX_PASSAGE_CHARS) -> str:
    """Hard-truncate a passage so query + passage fits the reranker window."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0]


def rerank_candidates(
    query: str,
    candidates: list[FusedCandidate],
    reranker: Reranker,
) -> list[tuple[FusedCandidate, float]]:
    """Rerank fused candidates with the cross-encoder; (candidate, score)
    sorted descending. Raises ValueError on a score-count mismatch."""
    if not candidates:
        return []
    passages = [truncate_to_budget(c.record.text) for c in candidates]
    scores = reranker.score(query, passages)
    if len(scores) != len(candidates):
        raise ValueError(
            f"reranker returned {len(scores)} scores for {len(candidates)} passages"
        )
    return sorted(zip(candidates, scores), key=lambda pair: pair[1], reverse=True)


class MiniLMReranker(Reranker):
    """Production reranker: cross-encoder/ms-marco-MiniLM-L6-v2, CPU.

    Loads from ``MINILM_MODEL_PATH`` (pre-staged artifact; the tokenizer
    defaults to the same path, overridable via ``MINILM_TOKENIZER_PATH``).
    Raises RuntimeError at construction when the artifact is missing —
    services fail startup rather than serving unranked results.
    """

    model_id: str = "cross-encoder/ms-marco-MiniLM-L6-v2"

    def __init__(
        self,
        model_path: str | None = None,
        tokenizer_path: str | None = None,
    ) -> None:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        self.model_path = model_path or os.environ.get("MINILM_MODEL_PATH", "")
        self.tokenizer_path = (
            tokenizer_path or os.environ.get("MINILM_TOKENIZER_PATH", "") or self.model_path
        )
        if not self.model_path or not os.path.isdir(self.model_path):
            raise RuntimeError(
                "MINILM_MODEL_PATH is not set or not a directory: the MiniLM "
                "cross-encoder must be pre-staged per the artifact policy "
                "(spec §14). Refusing to download."
            )
        if not os.path.isdir(self.tokenizer_path):
            raise RuntimeError(
                f"MINILM_TOKENIZER_PATH {self.tokenizer_path!r} is not a "
                "directory: refusing to download the tokenizer."
            )
        from sentence_transformers import CrossEncoder  # lazy: keeps tests import-light

        self._model = CrossEncoder(
            self.model_path, device="cpu", tokenizer_name=self.tokenizer_path
        )

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        pairs = [(query, passage) for passage in passages]
        raw = self._model.predict(pairs)
        return [float(s) for s in raw]
