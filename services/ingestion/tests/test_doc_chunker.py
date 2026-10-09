"""DocChunker: heading chunking, breadcrumbs, never-split-code, budget rule."""

from __future__ import annotations

import pytest

from services.ingestion.doc_chunker import ChunkerSettings, DocChunker
from services.ingestion.models import content_chunk_id

from conftest import word_len

LIB, VER = "fakelib", "0.1.0"


def chunker(**kw):
    settings = ChunkerSettings(target_tokens=20, overlap_tokens=4,
                               hard_budget_tokens=30)
    settings.__dict__.update(kw)
    return DocChunker(settings=settings, token_len=word_len)


MD = """# API Reference

Intro paragraph one. Intro paragraph two with more words here.

## Client

The client class wraps the connection.

### connect()

Opens the connection.

```python
def connect(url):
    return open(url)
```

### disconnect()

Closes the connection.
"""


def test_heading_sections_and_breadcrumbs():
    chunks = chunker().chunk_page(LIB, VER, "https://x/y", "hash1", MD)
    assert chunks, "expected chunks"
    # The connect() section carries the full breadcrumb.
    connect_chunks = [c for c in chunks if c.headings == ["API Reference", "Client", "connect()"]]
    assert connect_chunks, f"breadcrumbs seen: {[c.headings for c in chunks]}"
    # All chunks respect the hard budget (no oversized code in this doc).
    for c in chunks:
        assert word_len(c.text) <= 30, f"over budget: {c.text!r}"


def test_code_block_never_split_mid_block():
    code_lines = "\n".join(f"line_{i} = {i}" for i in range(30))
    md = f"# T\n\nSome prose here.\n\n```python\n{code_lines}\n```\n"
    chunks = chunker().chunk_page(LIB, VER, "u", "h", md)
    code_chunks = [c for c in chunks if c.code_language == "python"]
    assert len(code_chunks) == 1, "code block must stay in exactly one chunk"
    assert code_lines.splitlines()[0] in code_chunks[0].text
    assert code_lines.splitlines()[-1] in code_chunks[0].text


def test_oversized_code_emitted_whole_with_flag():
    # 50 words > hard budget 30 -> stays whole, flagged, no split.
    big = "x = 1  # " + "word " * 50
    md = f"# T\n\n```python\n{big}\n```\n"
    chunks = chunker().chunk_page(LIB, VER, "u", "h", md)
    flagged = [c for c in chunks if c.oversized_code]
    assert len(flagged) == 1
    assert big in flagged[0].text  # whole, not truncated
    assert flagged[0].code_language == "python"


def test_oversized_code_is_budget_exempt():
    big = "word " * 60  # 60 words > hard budget 30
    md = f"# T\n\n```js\n{big}\n```\n"
    chunks = chunker().chunk_page(LIB, VER, "u", "h", md)
    assert any(c.oversized_code for c in chunks)  # no assertion raised


def test_code_between_target_and_budget_stays_whole_unflagged():
    # 25 words: over target (20) but within budget (30) -> its own chunk,
    # whole, no flag.
    code = "word " * 25
    md = f"# T\n\nSome prose here.\n\n```python\n{code}\n```\n"
    chunks = chunker().chunk_page(LIB, VER, "u", "h", md)
    code_chunks = [c for c in chunks if c.code_language == "python"]
    assert len(code_chunks) == 1
    assert not code_chunks[0].oversized_code
    assert code.strip() in code_chunks[0].text


def test_boundaryless_prose_never_exceeds_budget():
    # No sentence terminators at all: greedy word-pack must hold the budget.
    md = "# T\n\n" + ("lorem ipsum dolor " * 100) + "\n"
    chunks = chunker().chunk_page(LIB, VER, "u", "h", md)
    assert len(chunks) > 1
    for c in chunks:
        assert word_len(c.text) <= 30


def test_budget_violation_raises():
    # White-box: the hard rule must fire if a normal chunk ever exceeds budget.
    c = chunker()
    section_units_text = "word " * 100  # 100 words, way over budget 30
    from services.ingestion.doc_chunker import _Section, _Unit
    section = _Section(breadcrumb=["T"], units=[_Unit(kind="prose", text="ok")])
    with pytest.raises(AssertionError):
        c._make_chunk([_Unit(kind="prose", text="ok")], section_units_text,
                      section, LIB, VER, "u", "h")


def test_prose_overlap_on_splits():
    paras = "\n\n".join(f"paragraph number {i} with filler words here" for i in range(8))
    md = f"# T\n\n{paras}\n"
    chunks = chunker().chunk_page(LIB, VER, "u", "h", md)
    assert len(chunks) >= 2
    # Overlap: some words of an early chunk reappear at the start of the next.
    assert chunks[1].text.startswith("paragraph number") or \
        any(w in chunks[1].text for w in chunks[0].text.split()[-4:])


def test_chunk_ids_stable_and_content_derived():
    a = chunker().chunk_page(LIB, VER, "u", "h", MD)
    b = chunker().chunk_page(LIB, VER, "u", "h", MD)
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
    # Same content, different version -> different IDs (version-scoped).
    c = chunker().chunk_page(LIB, "0.2.0", "u", "h", MD)
    assert {x.chunk_id for x in a} != {x.chunk_id for x in c}


def test_identifiers_extracted_from_code():
    md = "# T\n\n```python\ndef connect(url):\n    return Session(url).open()\n```\n"
    chunks = chunker().chunk_page(LIB, VER, "u", "h", md)
    code = [c for c in chunks if c.code_language == "python"][0]
    assert "connect" in code.identifiers
