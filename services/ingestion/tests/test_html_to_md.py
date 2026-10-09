"""html_to_markdown: code-block preservation, language tags, nav/footer drop."""

from __future__ import annotations

from services.ingestion.html_to_md import html_to_markdown

HTML = """<!DOCTYPE html>
<html><head><title>Demo</title></head>
<body>
<nav><ul><li>nav link one</li><li>nav link two</li></ul></nav>
<aside>sidebar promo text</aside>
<main>
<h1>Demo page</h1>
<p>Install it first, then run the snippet below.</p>
<pre><code class="language-python">def hello(name):
    return f"hello {name}"
</code></pre>
</main>
<footer>footer copyright 2026, do not index me</footer>
</body></html>"""


def test_code_block_preserved_verbatim_with_language():
    md = html_to_markdown(HTML, "https://example.com/demo")
    assert "```python" in md
    assert 'def hello(name):' in md
    assert 'return f"hello {name}"' in md


def test_nav_sidebar_footer_dropped():
    md = html_to_markdown(HTML, "https://example.com/demo")
    assert "nav link one" not in md
    assert "sidebar promo text" not in md
    assert "do not index me" not in md


def test_prose_kept():
    md = html_to_markdown(HTML, "https://example.com/demo")
    assert "Install it first" in md


def test_empty_input_returns_empty():
    assert html_to_markdown("", "https://example.com/") == ""
    assert html_to_markdown("   ", "https://example.com/") == ""
