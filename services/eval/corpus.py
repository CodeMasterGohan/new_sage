"""Starter eval corpus for new_sage (spec §16).

The corpus is hand-written JSONL: ~50 questions across 10 libraries × 2
versions, of the three spec types (how / changed / signature). The matching
``corpus/docs/<library>__<version>.md`` Markdown files carry the gold
passages VERBATIM (a test enforces this) plus distractor sections, so the
real ingestion chunker can be exercised end-to-end without any crawled data.

Extend to the full 150 by appending items to ``corpus/eval_corpus.jsonl``
and ensuring each new (library, version) has a doc in ``corpus/docs/``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"
CORPUS_FILE = CORPUS_DIR / "eval_corpus.jsonl"
DOCS_DIR = CORPUS_DIR / "docs"
RESOLVER_CASES_FILE = CORPUS_DIR / "resolver_cases.jsonl"

REQUIRED_FIELDS = (
    "id",
    "library",
    "version",
    "type",
    "question",
    "gold_passage_text",
    "gold_url",
    "gold_section",
)
QUESTION_TYPES = ("how", "changed", "signature")

_DOC_NAME_RE = re.compile(r"^([a-z0-9_]+)__(.+)\.md$")


@dataclass(frozen=True)
class EvalItem:
    """One eval question (spec §16: topic / version-discrimination / identifier)."""

    id: str
    library: str  # library_id as in libraries_seed.json
    version: str  # the version the question is asked against
    type: str  # "how" | "changed" | "signature"
    question: str
    gold_passage_text: str  # 1-4 sentences, verbatim in the version's doc
    gold_url: str  # real docs URL of the gold passage (provenance realism)
    gold_section: tuple  # heading breadcrumb of the gold section


@dataclass(frozen=True)
class EvalDoc:
    """One hand-written Markdown doc backing a (library, version) pair."""

    library: str
    version: str
    url: str  # synthetic page URL used at chunk time
    markdown: str

    @property
    def page_hash(self) -> str:
        return hashlib.sha256(self.markdown.encode("utf-8")).hexdigest()[:16]


def validate_item(raw: dict) -> EvalItem:
    """Validate one raw JSONL row; raise ValueError describing the problem."""
    missing = [f for f in REQUIRED_FIELDS if f not in raw]
    if missing:
        raise ValueError(f"item missing fields {missing}: {raw!r}")
    for field in REQUIRED_FIELDS:
        if field == "gold_section":
            continue
        if not isinstance(raw[field], str) or not raw[field].strip():
            raise ValueError(f"item field {field!r} must be a non-empty string")
    if raw["type"] not in QUESTION_TYPES:
        raise ValueError(f"item type must be one of {QUESTION_TYPES}, got {raw['type']!r}")
    if not isinstance(raw["gold_section"], list) or not all(
        isinstance(s, str) and s.strip() for s in raw["gold_section"]
    ):
        raise ValueError("gold_section must be a non-empty list of strings")
    return EvalItem(
        id=raw["id"],
        library=raw["library"],
        version=raw["version"],
        type=raw["type"],
        question=raw["question"],
        gold_passage_text=raw["gold_passage_text"],
        gold_url=raw["gold_url"],
        gold_section=tuple(raw["gold_section"]),
    )


def load_corpus(path: str | Path = CORPUS_FILE) -> list[EvalItem]:
    """Load and schema-validate every item in the JSONL corpus."""
    items: list[EvalItem] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                item = validate_item(json.loads(line))
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: {exc}") from exc
            if item.id in seen:
                raise ValueError(f"{path}:{lineno}: duplicate id {item.id!r}")
            seen.add(item.id)
            items.append(item)
    if not items:
        raise ValueError(f"corpus is empty: {path}")
    return items


def load_docs(docs_dir: str | Path = DOCS_DIR) -> list[EvalDoc]:
    """Load every ``<library>__<version>.md`` doc in the docs directory."""
    docs: list[EvalDoc] = []
    for path in sorted(Path(docs_dir).glob("*.md")):
        match = _DOC_NAME_RE.match(path.name)
        if not match:
            raise ValueError(f"doc filename must be <library>__<version>.md: {path.name}")
        library, version = match.groups()
        markdown = path.read_text(encoding="utf-8")
        if not markdown.strip():
            raise ValueError(f"doc is empty: {path.name}")
        docs.append(
            EvalDoc(
                library=library,
                version=version,
                url=f"https://docs.new-sage.local/{library}/{version}/",
                markdown=markdown,
            )
        )
    if not docs:
        raise ValueError(f"no docs found in {docs_dir}")
    return docs


def load_resolver_cases(
    path: str | Path = RESOLVER_CASES_FILE,
) -> list[dict]:
    """Load resolver accuracy cases: {query, ecosystem?, expected_library_id?}."""
    cases: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if "query" not in row or not str(row["query"]).strip():
                raise ValueError(f"{path}:{lineno}: case needs a non-empty 'query'")
            cases.append(row)
    if not cases:
        raise ValueError(f"resolver cases file is empty: {path}")
    return cases
