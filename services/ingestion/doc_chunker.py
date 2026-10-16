"""Heading-based chunker (spec §7).

Docs chunk by structure, not by page count:

- Split Markdown on H1/H2/H3 into sections; sections longer than
  ~420 tokens split further on paragraph boundaries.
- Code blocks are NEVER split mid-block; a section that is one large code
  block stays whole. A single code block >2000 tokens is emitted whole with
  ``oversized_code=True`` (warning in provenance) — never silently
  truncated.
- 48-token overlap on prose splits.

HARD RULE (adapted from new_DRUID's budget discipline): every emitted
chunk (except flagged oversized code) must fit the hard budget
(``hard_budget_tokens``, default 512). Violation raises — it is a bug in
the splitter, not a warning. Oversized code is the one documented
exemption and carries the ``oversized_code`` flag.

The tokenizer is injected (``token_len`` callable) so the chunker never
downloads anything: production wires the pre-staged MiniLM tokenizer,
tests inject a deterministic counter.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from dataclasses import dataclass, field

from .models import Chunk, content_chunk_id

# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


@dataclass
class ChunkerSettings:
    target_tokens: int = 420  # prose packing target (spec §7, §17.4)
    overlap_tokens: int = 48  # prose overlap on splits
    hard_budget_tokens: int = 512  # assertion budget for normal chunks

    def __post_init__(self) -> None:
        if self.target_tokens > self.hard_budget_tokens:
            raise ValueError(
                f"target_tokens ({self.target_tokens}) must not exceed "
                f"hard_budget_tokens ({self.hard_budget_tokens})"
            )


# ---------------------------------------------------------------------------
# tokenizer
# ---------------------------------------------------------------------------


@lru_cache(maxsize=4)
def _load_tokenizer(path: str):
    """Load each offline tokenizer path only once per worker process."""
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("transformers is required for the production token_len") from exc
    return AutoTokenizer.from_pretrained(
        path, local_files_only=True, trust_remote_code=False
    )


def _default_token_len(text: str) -> int:
    """Count tokens using the pre-staged cached MiniLM tokenizer."""
    path = os.environ.get("MINILM_TOKENIZER_PATH", "")
    if not path:
        raise RuntimeError("MINILM_TOKENIZER_PATH must refer to a pre-staged model")
    return len(_load_tokenizer(path).encode(text, add_special_tokens=False))


# ---------------------------------------------------------------------------
# markdown parsing: sections + units
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^(\s*)(```+|~~~+)\s*([^\s`~]*)\s*$")


@dataclass
class _Unit:
    kind: str  # "prose" | "code"
    text: str
    lang: str | None = None


@dataclass
class _Section:
    breadcrumb: list[str]
    units: list[_Unit] = field(default_factory=list)


def _parse_sections(markdown: str) -> list[_Section]:
    """Split markdown into sections on H1/H2/H3; collect prose/code units."""
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []  # (level, title)

    def current() -> _Section:
        if not sections:
            sections.append(_Section(breadcrumb=[t for _, t in stack]))
        return sections[-1]

    lines = markdown.splitlines()
    buf: list[str] = []
    in_code = False
    fence_char = ""
    fence_len = 0
    code_lang: str | None = None
    code_lines: list[str] = []

    def flush_prose() -> None:
        text = "\n".join(buf).strip()
        if text:
            current().units.append(_Unit(kind="prose", text=text))
        buf.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence and not in_code:
            flush_prose()
            in_code = True
            fence_char = fence.group(2)[0]
            fence_len = len(fence.group(2))
            code_lang = fence.group(3) or None
            code_lines = []
            i += 1
            continue
        if in_code:
            close = re.match(rf"^\s*{re.escape(fence_char)}{{{fence_len},}}\s*$", line)
            if close:
                current().units.append(
                    _Unit(kind="code", text="\n".join(code_lines), lang=code_lang)
                )
                in_code = False
                code_lang = None
            else:
                code_lines.append(line)
            i += 1
            continue
        # Unterminated fence at EOF degrades to a code unit (never dropped).
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            if level <= 3:
                # H1/H2/H3 starts a new section.
                flush_prose()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                sections.append(_Section(breadcrumb=[t for _, t in stack]))
            else:
                # H4+ stays in-section; the line is kept in prose.
                buf.append(line)
            i += 1
            continue
        # Setext headings: "Title\n===" or "Title\n---"
        if (
            i + 1 < len(lines)
            and line.strip()
            and not line.startswith(" ")
            and re.match(r"^=+\s*$", lines[i + 1])
        ):
            flush_prose()
            title = line.strip()
            while stack:
                stack.pop()
            stack.append((1, title))
            sections.append(_Section(breadcrumb=[title]))
            i += 2
            continue
        if (
            i + 1 < len(lines)
            and line.strip()
            and not line.startswith(" ")
            and re.match(r"^-+\s*$", lines[i + 1])
            and len(line.strip()) > 0
        ):
            # Guard: a "---" underline could also be a horizontal rule after
            # blank line; only treat as setext when the previous line is text
            # on the immediately preceding line (which it is here).
            flush_prose()
            title = line.strip()
            while stack and stack[-1][0] >= 2:
                stack.pop()
            stack.append((2, title))
            sections.append(_Section(breadcrumb=[t for _, t in stack]))
            i += 2
            continue
        buf.append(line)
        i += 1

    if in_code:
        # Unterminated fence: emit what we have, never drop content.
        current().units.append(
            _Unit(kind="code", text="\n".join(code_lines), lang=code_lang)
        )
    else:
        flush_prose()
    # Drop empty leading section if the doc starts with a heading.
    return [s for s in sections if s.units or s.breadcrumb]


# ---------------------------------------------------------------------------
# chunker
# ---------------------------------------------------------------------------


class DocChunker:
    """Structure-first, tokenizer-aware chunker for Markdown docs."""

    def __init__(
        self,
        settings: ChunkerSettings | None = None,
        token_len=None,  # Callable[[str], int]
    ) -> None:
        self.settings = settings or ChunkerSettings()
        self.token_len = token_len or _default_token_len

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def chunk_page(
        self,
        library_id: str,
        version: str,
        page_url: str,
        page_hash: str,
        markdown: str,
    ) -> list[Chunk]:
        """Chunk one page's Markdown into retrieval units."""
        chunks: list[Chunk] = []
        order = 0
        for section in _parse_sections(markdown):
            for chunk in self._chunk_section(
                section, library_id, version, page_url, page_hash, order
            ):
                chunk.chunk_order = order
                order += 1
                chunks.append(chunk)
        return chunks

    # ------------------------------------------------------------------
    # section packing
    # ------------------------------------------------------------------
    def _chunk_section(
        self,
        section: _Section,
        library_id: str,
        version: str,
        page_url: str,
        page_hash: str,
        start_order: int,
    ):
        units = [u for u in section.units if u.text.strip()]
        if not units:
            return
        # Fast path: whole section fits the target.
        if all(u.kind != "code" or self.token_len(u.text) <= self.settings.target_tokens for u in units):
            whole = self._render(units)
            if self.token_len(whole) <= self.settings.target_tokens:
                yield self._make_chunk(
                    units, whole, section, library_id, version, page_url, page_hash
                )
                return
        buf: list[_Unit] = []
        buf_tokens = 0
        for unit in units:
            u_tokens = self.token_len(
                self._render([unit]) if unit.kind == "code" else unit.text
            )
            if unit.kind == "code" and u_tokens > self.settings.hard_budget_tokens:
                # Unsplittable and over budget: emit whole with the
                # oversized_code flag (warning in provenance) — never split,
                # never silently truncate. This is the one documented
                # exemption from the budget assertion. (Spec §7's ">2000
                # tokens" is the extreme case; any over-budget code block is
                # equally unsplittable, so the operative threshold is the
                # budget itself.)
                if buf:
                    yield self._flush(buf, section, library_id, version, page_url, page_hash)
                    buf, buf_tokens = [], 0
                yield self._make_chunk(
                    [unit],
                    self._render([unit]),
                    section,
                    library_id,
                    version,
                    page_url,
                    page_hash,
                    oversized_code=True,
                )
                continue
            if u_tokens > self.settings.target_tokens:
                # Oversized single unit that still fits the budget
                # (prose, or a large-but-fittable code block): its own chunk.
                if buf:
                    yield self._flush(buf, section, library_id, version, page_url, page_hash)
                    buf, buf_tokens = [], 0
                if unit.kind == "code":
                    yield self._make_chunk(
                        [unit], self._render([unit]), section,
                        library_id, version, page_url, page_hash,
                    )
                else:
                    yield from self._split_long_prose(
                        unit, section, library_id, version, page_url, page_hash
                    )
                continue
            if buf and buf_tokens + u_tokens > self.settings.target_tokens:
                yield self._flush(buf, section, library_id, version, page_url, page_hash)
                # Overlap: carry ~overlap tokens of trailing PROSE forward —
                # but only if it still fits the hard budget with the incoming
                # unit. A target-sized tail must not blow the next chunk.
                tail = self._overlap_tail(buf)
                tail_tokens = sum(self.token_len(u.text) for u in tail)
                if tail_tokens + u_tokens <= self.settings.hard_budget_tokens:
                    buf, buf_tokens = tail, tail_tokens
                else:
                    buf, buf_tokens = [], 0
            if buf and buf_tokens + u_tokens > self.settings.hard_budget_tokens:
                # Backstop: the packing above should already guarantee this;
                # never emit an over-budget chunk.
                yield self._flush(buf, section, library_id, version, page_url, page_hash)
                buf, buf_tokens = [], 0
            buf.append(unit)
            buf_tokens += u_tokens
        if buf:
            yield self._flush(buf, section, library_id, version, page_url, page_hash)

    def _split_long_prose(
        self, unit: _Unit, section: _Section, library_id: str,
        version: str, page_url: str, page_hash: str,
    ):
        """Split an oversized prose unit on sentence boundaries."""
        sentences = re.split(r"(?<=[.!?])\s+", unit.text)
        buf: list[str] = []
        buf_tokens = 0
        for sent in sentences:
            s_tokens = self.token_len(sent)
            if s_tokens > self.settings.hard_budget_tokens:
                # Pathological: one giant sentence with no usable boundary
                # (or a tokenizer where chars/token < 4, which char-slicing
                # would not respect). Greedy word-pack to the hard budget;
                # a single monstrous word falls back to char slices. The
                # budget assertion in _make_chunk stays the backstop.
                if buf:
                    yield self._make_chunk(
                        [unit], " ".join(buf), section,
                        library_id, version, page_url, page_hash,
                    )
                    buf, buf_tokens = [], 0
                yield from self._pack_words(
                    unit, sent, section, library_id, version, page_url, page_hash
                )
                continue
            if buf and buf_tokens + s_tokens > self.settings.target_tokens:
                yield self._make_chunk(
                    [unit], " ".join(buf), section,
                    library_id, version, page_url, page_hash,
                )
                buf, buf_tokens = [], 0
            buf.append(sent)
            buf_tokens += s_tokens
        if buf:
            yield self._make_chunk(
                [unit], " ".join(buf), section,
                library_id, version, page_url, page_hash,
            )

    def _pack_words(
        self, unit: _Unit, text: str, section: _Section, library_id: str,
        version: str, page_url: str, page_hash: str,
    ):
        """Greedy word-pack for boundary-less text (never exceeds budget)."""
        budget = self.settings.hard_budget_tokens
        wbuf: list[str] = []
        wbuf_tokens = 0
        for word in text.split():
            wt = self.token_len(word)
            if wt > budget:
                if wbuf:
                    yield self._make_chunk(
                        [unit], " ".join(wbuf), section,
                        library_id, version, page_url, page_hash,
                    )
                    wbuf, wbuf_tokens = [], 0
                approx = budget * 4  # last resort for a monstrous single word
                for i in range(0, len(word), approx):
                    yield self._make_chunk(
                        [unit], word[i : i + approx], section,
                        library_id, version, page_url, page_hash,
                    )
                continue
            # +1 approximates the joining space; the budget assertion is the
            # backstop for tokenizers where this approximation is off.
            if wbuf and wbuf_tokens + wt + 1 > budget:
                yield self._make_chunk(
                    [unit], " ".join(wbuf), section,
                    library_id, version, page_url, page_hash,
                )
                wbuf, wbuf_tokens = [], 0
            wbuf.append(word)
            wbuf_tokens += wt + (1 if len(wbuf) > 1 else 0)
        if wbuf:
            yield self._make_chunk(
                [unit], " ".join(wbuf), section,
                library_id, version, page_url, page_hash,
            )

    def _overlap_tail(self, buf: list[_Unit]) -> list[_Unit]:
        """Carry ~overlap tokens of trailing PROSE context forward.

        Code blocks never participate in overlap (they are emitted
        whole and never re-emitted).
        """
        tail: list[_Unit] = []
        total = 0
        for unit in reversed(buf):
            if unit.kind == "code":
                break
            total += self.token_len(unit.text)
            tail.insert(0, unit)
            if total >= self.settings.overlap_tokens:
                break
        return tail

    # ------------------------------------------------------------------
    # builders
    # ------------------------------------------------------------------
    def _render(self, units: list[_Unit]) -> str:
        parts: list[str] = []
        for u in units:
            if u.kind == "code":
                fence = f"```{u.lang}" if u.lang else "```"
                parts.append(f"{fence}\n{u.text}\n```")
            else:
                parts.append(u.text)
        return "\n\n".join(parts)

    def _flush(
        self, buf: list[_Unit], section: _Section, library_id: str,
        version: str, page_url: str, page_hash: str,
    ) -> Chunk:
        return self._make_chunk(
            buf, self._render(buf), section,
            library_id, version, page_url, page_hash,
        )

    def _make_chunk(
        self,
        units: list[_Unit],
        text: str,
        section: _Section,
        library_id: str,
        version: str,
        page_url: str,
        page_hash: str,
        oversized_code: bool = False,
    ) -> Chunk:
        from .identifiers import extract_code_identifiers

        n = self.token_len(text)
        if not oversized_code:
            # HARD RULE: split, never truncate. A violation is a bug.
            assert n <= self.settings.hard_budget_tokens, (
                f"chunk over budget: {n} tokens > {self.settings.hard_budget_tokens}. "
                "Splitting logic failed to split — this is a bug."
            )
        langs = [u.lang for u in units if u.kind == "code" and u.lang]
        has_code = any(u.kind == "code" for u in units)
        chunk = Chunk(
            chunk_id=content_chunk_id(library_id, version, page_hash, text),
            library_id=library_id,
            version_id=version,
            page_id=page_hash,
            url=page_url,
            chunk_order=0,  # assigned by caller
            text=text,
            token_count=n,
            headings=list(section.breadcrumb),
            code_language=langs[0] if langs else ("code" if has_code else None),
            oversized_code=oversized_code,
            identifiers=extract_code_identifiers(text) if has_code else [],
        )
        return chunk
