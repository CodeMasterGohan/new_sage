"""HTML -> Markdown extraction (spec §5.3).

trafilatura first (best at dropping nav/sidebars/footers), falling back to
readability-lxml when trafilatura returns nothing. Code blocks are preserved
verbatim, fenced, with language tags kept. Pure function, no network.
"""

from __future__ import annotations

import logging

log = logging.getLogger("sage.ingestion")


def html_to_markdown(html: str, url: str) -> str:
    """Extract main content as Markdown.

    Args:
        html: raw HTML source.
        url: page URL (used by trafilatura for context; also embedded in
            provenance by the caller).

    Returns:
        Markdown text, or "" when nothing extractable was found.
    """
    if not html or not html.strip():
        return ""
    md = _trafilatura_extract(html, url)
    if md and md.strip():
        return _normalize(_reattach_code_languages(html, md))
    md = _readability_extract(html, url)
    return _normalize(md)


def _code_languages(html: str) -> list[str | None]:
    """Languages of <pre><code class="language-x"> blocks, in document order."""
    try:
        from lxml import html as lxml_html
    except ImportError:
        return []
    try:
        tree = lxml_html.fromstring(html)
    except Exception as exc:
        log.debug("lxml parse failed for code-language scan: %s", exc)
        return []
    langs: list[str | None] = []
    for pre in tree.iter("pre"):
        lang: str | None = None
        for code in pre.iter("code"):
            for cls in (code.get("class") or "").split():
                if cls.startswith("language-"):
                    lang = cls[len("language-") :]
                    break
            break  # first <code> child carries the language
        langs.append(lang)
    return langs


def _reattach_code_languages(html: str, md: str) -> str:
    """trafilatura emits bare ``` fences; re-attach language tags.

    The source HTML's <pre> blocks and the extracted fences are in the same
    document order, so languages are assigned sequentially to unlabeled
    opening fences. Spec §5.3: language tags matter for BM25 and readers.
    """
    langs = _code_languages(html)
    if not any(langs):
        return md
    out: list[str] = []
    idx = 0
    for line in md.splitlines():
        stripped = line.strip()
        if stripped == "```" and idx < len(langs):
            lang = langs[idx]
            idx += 1
            out.append(f"```{lang}" if lang else line)
        else:
            out.append(line)
    return "\n".join(out)


def _trafilatura_extract(html: str, url: str) -> str:
    try:
        import trafilatura
    except ImportError:
        return ""
    return trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        include_images=False,
        include_links=False,
        favor_precision=True,
    ) or ""


def _readability_extract(html: str, url: str) -> str:
    try:
        from readability import Document
        from lxml import html as lxml_html
    except ImportError:
        return ""
    try:
        doc = Document(html, url=url)
        article_html = doc.summary()
    except Exception as exc:
        log.debug("readability extract failed for %s: %s", url, exc)
        return ""
    try:
        tree = lxml_html.fromstring(article_html)
    except Exception as exc:
        log.debug("readability output unparsable for %s: %s", url, exc)
        return ""
    return _tree_to_markdown(tree)


def _tree_to_markdown(tree) -> str:
    """Minimal HTML->Markdown for the readability fallback path.

    Preserves headings, paragraphs, lists, and fenced code blocks (with
    language tags when the source carries a class like ``language-python``);
    everything else is flattened to text.
    """
    parts: list[str] = []

    def lang_of(el) -> str:
        classes = (el.get("class") or "").split()
        for c in classes:
            if c.startswith("language-"):
                return c[len("language-") :]
        return ""

    def text(el) -> str:
        return "".join(el.itertext()).strip()

    for el in tree.iter():
        tag = (el.tag or "").lower() if isinstance(el.tag, str) else ""
        if tag == "pre":
            code = text(el)
            code_el = el.find(".//code")
            lang = lang_of(code_el) if code_el is not None else lang_of(el)
            fence = f"```{lang}" if lang else "```"
            parts.append(f"{fence}\n{code}\n```")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            parts.append(f"{'#' * level} {text(el)}")
        elif tag == "p":
            # Skip paragraphs that are just the code we already fenced.
            if el.find(".//code") is None and not any(
                a.tag == "pre" for a in el.iterancestors()
            ):
                t = text(el)
                if t:
                    parts.append(t)
        elif tag == "li":
            t = text(el)
            if t:
                parts.append(f"- {t}")

    # Deduplicate: paragraphs inside nested elements may repeat text.
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return "\n\n".join(out)


def _normalize(md: str) -> str:
    """Trim trailing whitespace per line; collapse 3+ blank lines."""
    import re

    lines = [line.rstrip() for line in md.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
