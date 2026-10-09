"""Corpus schema validation: every item complete, gold passages sane,
and the (library, version) doc coverage the harness depends on."""

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from corpus import (  # noqa: E402
    CORPUS_FILE,
    DOCS_DIR,
    REQUIRED_FIELDS,
    load_corpus,
    load_docs,
    load_resolver_cases,
)


def _sentence_count(text: str) -> int:
    cleaned = re.sub(r"\d\.\d*", "", text)  # decimals / version numbers
    cleaned = re.sub(r"\b(?:[A-Z]\.)+[A-Z]?\b", "", cleaned)  # abbreviations
    return len(re.findall(r"[.!?]+(?=\s|$)", cleaned))


def test_corpus_loads_and_has_expected_size():
    items = load_corpus(CORPUS_FILE)
    assert len(items) == 50


def test_corpus_covers_ten_libraries_times_two_versions():
    items = load_corpus(CORPUS_FILE)
    pairs = {(it.library, it.version) for it in items}
    assert len(pairs) == 20
    libs = {lib for lib, _ in pairs}
    assert len(libs) == 10
    for lib in libs:
        assert sum(1 for l, _ in pairs if l == lib) == 2


def test_question_type_mix():
    items = load_corpus(CORPUS_FILE)
    counts = Counter(it.type for it in items)
    assert counts["how"] == 20
    assert counts["changed"] == 10
    assert counts["signature"] == 20


def test_every_item_has_all_fields_nonempty():
    items = load_corpus(CORPUS_FILE)
    for it in items:
        for f in REQUIRED_FIELDS:
            assert getattr(it, f), f"{it.id}: empty {f}"


def test_gold_passages_are_short_and_nonempty():
    items = load_corpus(CORPUS_FILE)
    for it in items:
        n = _sentence_count(it.gold_passage_text)
        assert 1 <= n <= 4, f"{it.id}: {n} sentences"
        assert len(it.gold_passage_text) <= 600, f"{it.id}: passage too long"


def test_gold_urls_look_real():
    items = load_corpus(CORPUS_FILE)
    for it in items:
        assert it.gold_url.startswith("https://"), f"{it.id}: bad url"


def test_docs_cover_every_item_pair():
    items = load_corpus(CORPUS_FILE)
    docs = {(d.library, d.version) for d in load_docs(DOCS_DIR)}
    for it in items:
        assert (it.library, it.version) in docs, f"{it.id}: no doc"


def test_gold_passage_verbatim_in_doc():
    items = load_corpus(CORPUS_FILE)
    by_pair = {(d.library, d.version): d.markdown for d in load_docs(DOCS_DIR)}
    for it in items:
        md = by_pair[(it.library, it.version)]
        assert it.gold_passage_text in md, f"{it.id}: gold passage not verbatim"


def test_resolver_cases_load():
    cases = load_resolver_cases()
    assert len(cases) >= 30
    assert all(c["query"].strip() for c in cases)
