"""Parity checks for opt-in Rust Markdown processing.

Rust must retain heading breadcrumbs, fenced code and content hashes. The
normal Python installation intentionally skips this file until the native
maturin wheel is installed by the Rust CI job.
"""
import random
import statistics
import time

import pytest

native = pytest.importorskip("sage_markdown_fastpath")

from services.ingestion.doc_chunker import _parse_sections
from services.ingestion.html_to_md import _normalize


def _canon(sections):
    return [(s.breadcrumb, [(u.kind, u.text, u.lang) for u in s.units])
            for s in sections]


@pytest.mark.parametrize("markdown", [
    "",
    "hello world",
    "# Heading\nBody paragraph\n\nMore words.",
    "# H1\n## H2\n### H3\n#### H4 stays prose\nend",
    "Title\n===\nOne paragraph.\nSubtitle\n---\nMore text.",
    "# Python\n\n```python\nprint('hello')\n```\n\nAfter code.",
    "~~~js\nconsole.log('hello')\n~~~",
    "Title\n\n```rust\nfn main() {}\n",
    "# Português 東京\nemoji 🚀\n\n```go\nfmt.Print(1)\n```",
    "before\r\n# CRLF heading\r\nafter\r\n",
    "Leading\n\n\n\nTrailing\n",
    "# Heading\n\nhello\n\n## Another\n\nParagraph\n",
])
def test_markdown_structure_and_normalization_parity(monkeypatch, markdown):
    monkeypatch.setenv("SAGE_RUST_FASTPATH", "0")
    python_sections = _canon(_parse_sections(markdown))
    python_normal = _normalize(markdown)
    monkeypatch.setenv("SAGE_RUST_FASTPATH", "1")
    rust_sections = _canon(_parse_sections(markdown))
    rust_normal = _normalize(markdown)
    assert rust_sections == python_sections
    assert rust_normal == python_normal


@pytest.mark.parametrize("markdown", [
    "text\rwith standalone carriage return",
    "unicode\u2028separator",
    "tabs\t  \n\n\nmore text",
    "\x0cform feed\nparagraph",
])
def test_unusual_line_separators_safely_fallback_to_python(monkeypatch, markdown):
    monkeypatch.setenv("SAGE_RUST_FASTPATH", "0")
    original = (_canon(_parse_sections(markdown)), _normalize(markdown))
    monkeypatch.setenv("SAGE_RUST_FASTPATH", "1")
    rust_opt = (_canon(_parse_sections(markdown)), _normalize(markdown))
    assert rust_opt == original


def test_document_equivalence_on_synthetic_corpus(monkeypatch):
    rng = random.Random(31)
    sections = []
    for i in range(250):
        para = " ".join(rng.choice(["Rust", "Python", "PDF", "index", "文書", "token"])
                        for _ in range(90))
        sections.append(f"## Heading {i}\n{para}\n\n```python\nprint({i})\n```")
    md = "# Documentation\n\n" + "\n\n".join(sections)
    monkeypatch.setenv("SAGE_RUST_FASTPATH", "0")
    canonical = _canon(_parse_sections(md))
    normalized = _normalize(md)
    monkeypatch.setenv("SAGE_RUST_FASTPATH", "1")
    accelerated = _canon(_parse_sections(md))
    assert accelerated == canonical
    assert _normalize(md) == normalized


def _median_ms(fn, n=12):
    values=[]
    for _ in range(n):
        t0=time.perf_counter_ns()
        fn()
        values.append((time.perf_counter_ns()-t0)/1e6)
    return statistics.median(values)


def test_markdown_cpu_microbenchmark(monkeypatch, capsys):
    md = ("# Library\n" +
          ("## Title\nHello **world**.\n\n```rust\nfn main() {}\n```\n\n" * 1600))
    for mode in ("0", "1"):
        monkeypatch.setenv("SAGE_RUST_FASTPATH", mode)
        _parse_sections(md)
        _normalize(md)
        print(f"Sage section parse mode={mode} median_ms={_median_ms(lambda: _parse_sections(md), 7):.3f}")
        print(f"Sage normalize mode={mode} median_ms={_median_ms(lambda: _normalize(md), 7):.3f}")
