"""Library resolver for ``resolve-library-id`` (spec §9.3).

Resolution order:

1. Exact match on canonical name or aliases (normalized; identifier index).
2. Ecosystem-aware disambiguation — an optional ``ecosystem`` hint picks
   between records that share a name/alias.
3. Fuzzy fallback (BM25-over-names scoring) returning scored alternatives.
4. Unknown → ``{found: false, message: "unknown library",
   hint: "supply docs_url to register"}``.

P1 note (§18): the resolver reads the hand-written seed table
(``libraries_seed.json``). The catalog service (sibling agent, P2+) owns
the ``library`` table in Postgres; when it lands, swap ``records`` for a
catalog-backed loader — the ``LibraryResolver`` interface takes plain
:class:`LibraryRecord` rows either way.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from .models import Alternative, LibraryRecord, ResolutionResult

log = logging.getLogger(__name__)

# Fuzzy auto-accept threshold: only a strong fuzzy hit resolves without
# asking. Below it we return scored alternatives (Context7-style ambiguity).
_FUZZY_AUTO_SCORE = 3.0
_FUZZY_MIN_SCORE = 0.5
_MAX_ALTERNATIVES = 4

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _normalize(value: str) -> str:
    """Lowercase, collapse separators — 'React-DOM', 'react_dom', 'reactdom'
    all compare equal for exact matching."""
    return re.sub(r"[\s_\-.]+", "", value.strip().lower())


def _tokens(value: str) -> list[str]:
    return _TOKEN_RE.findall(value.lower())


def _trigrams(value: str) -> set[str]:
    """Character trigrams of the normalized string (typo-tolerant tier)."""
    v = _normalize(value)
    if len(v) < 3:
        return {v} if v else set()
    padded = f"^{v}$"
    grams = {padded[i : i + 3] for i in range(len(padded) - 2)}
    if len(v) <= 6:
        grams.update(padded[i : i + 2] for i in range(len(padded) - 1))
    return grams


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Weight of the trigram-similarity tier in the fuzzy score.
_TRIGRAM_WEIGHT = 2.0


class LibraryResolver:
    def __init__(self, records: list[LibraryRecord]) -> None:
        self._records = list(records)
        # Identifier index: normalized name/alias -> records (§9.3 step 1).
        self._exact: dict[str, list[LibraryRecord]] = {}
        for record in self._records:
            for key in {_normalize(record.name), *(_normalize(a) for a in record.aliases)}:
                self._exact.setdefault(key, []).append(record)

    @classmethod
    def from_seed_file(cls, path: str | Path) -> "LibraryResolver":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        records = [LibraryRecord(**row) for row in data["libraries"]]
        log.info("resolver: loaded %d records from %s", len(records), path)
        return cls(records)

    @classmethod
    def from_catalog(cls, database_url: str) -> "LibraryResolver":
        """Build a resolver from the Postgres catalog (Wave 2 DB seam).

        Reads ``library`` rows via ``services.catalog`` and converts them
        to :class:`LibraryRecord` rows (``library_id`` = canonical slug =
        ``Library.name``; ``versions``/``latest`` from the version rows).
        The file-backed seed table stays the default; call this explicitly
        when the catalog is populated.

        Deployment note: the mcp-server image copies in ``services/catalog``
        and ships ``sqlalchemy``/``psycopg`` (Wave 5 Docker fix), so this
        path runs in Docker as long as ``database_url`` is reachable.
        """
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from services.catalog import CatalogRepository

        engine = create_engine(database_url)
        records: list[LibraryRecord] = []
        with Session(engine) as session:
            repo = CatalogRepository(session)
            for lib in repo.list_libraries(limit=10000):
                ver_rows = repo.list_versions(lib.id)
                # Dedupe: one entry per version string — multiple immutable
                # revisions (spec §4) collapse to a single version here;
                # get-library-docs always serves the active revision.
                versions = list(dict.fromkeys(v.version for v in ver_rows))
                latest_row = next((v for v in ver_rows if v.is_latest), None) or next((v for v in ver_rows if getattr(v, "status", None) == "ready"), None)
                latest = (
                    latest_row.version
                    if latest_row is not None
                    else (versions[-1] if versions else "")
                )
                records.append(
                    LibraryRecord(
                        library_id=lib.name,
                        name=lib.name,
                        aliases=list(lib.aliases or []),
                        ecosystem=lib.ecosystem or "",
                        docs_url=lib.docs_url or "",
                        repo_url=lib.repo_url or "",
                        versions=versions,
                        latest=latest,
                        description=lib.resolver_notes or "",
                    )
                )
        engine.dispose()
        # NOTE: the URL is deliberately NOT logged (it may carry credentials).
        log.info("resolver: loaded %d records from the catalog", len(records))
        return cls(records)

    def get(self, library_id: str) -> LibraryRecord | None:
        """Look up a record by exact library_id (for get-library-docs)."""
        wanted = _normalize(library_id)
        for record in self._records:
            if _normalize(record.library_id) == wanted:
                return record
        return None

    def get_by_context7_id(self, value: str):
        """Resolve a known GitHub-style /owner/repo[/version] ID.

        Only registered repository URLs are eligible; never fetch arbitrary
        user-supplied URLs. Legacy canonical library IDs also work.
        """
        raw = value.strip()
        if not raw.startswith("/"):
            return self.get(raw), None
        parts = [part for part in raw.split("/") if part]
        if len(parts) not in (2, 3):
            return None, None
        owner, repo = parts[:2]
        version = parts[2] if len(parts) == 3 else None
        if "@" in repo:
            repo, suffix = repo.rsplit("@", 1)
            version = version or suffix
        project = f"{owner}/{repo}".lower()
        for record in self._records:
            if record.repo_url:
                path = urlsplit(record.repo_url).path.strip("/").removesuffix(".git").lower()
                if path == project:
                    return record, version
        return None, None

    # -- resolution ---------------------------------------------------------

    def resolve(
        self, library_name: str, ecosystem: str | None = None
    ) -> ResolutionResult:
        """Resolve a free-text library name per §9.3."""
        key = _normalize(library_name)
        eco = _normalize(ecosystem) if ecosystem else None

        # 1+2. Exact match, with ecosystem-aware disambiguation.
        # (Dedupe: a record whose name equals one of its aliases appears
        # under the same key twice.)
        hits: list[LibraryRecord] = []
        seen: set[int] = set()
        for record in self._exact.get(key, []):
            if id(record) not in seen:
                seen.add(id(record))
                hits.append(record)
        if hits:
            if len(hits) == 1:
                log.debug("resolver: exact match %r -> %s", library_name, hits[0].library_id)
                return self._found(hits[0], alternatives=[])
            if eco:
                narrowed = [
                    r for r in hits if _normalize(r.ecosystem) == eco
                ]
                if len(narrowed) == 1:
                    log.debug(
                        "resolver: exact match %r -> %s (ecosystem=%s)",
                        library_name,
                        narrowed[0].library_id,
                        ecosystem,
                    )
                    return self._found(narrowed[0], alternatives=[])
                if narrowed:
                    hits = narrowed  # still ambiguous within the ecosystem
            log.info(
                "resolver: ambiguous %r -> %d candidates %s",
                library_name,
                len(hits),
                [r.library_id for r in hits],
            )
            return ResolutionResult(
                found=False,
                message=(
                    f"ambiguous library name '{library_name}': matches "
                    f"{len(hits)} libraries"
                ),
                hint="re-run with an ecosystem hint (e.g. ecosystem='pypi') "
                "to disambiguate",
                alternatives=[
                    Alternative(library_id=r.library_id, name=r.name, score=1.0)
                    for r in hits
                ],
            )

        # 3. Fuzzy BM25-over-names fallback.
        scored = self._fuzzy(library_name)
        if scored and scored[0][1] >= _FUZZY_AUTO_SCORE:
            top, _ = scored[0]
            rest = [
                Alternative(library_id=r.library_id, name=r.name, score=s)
                for r, s in scored[1 : _MAX_ALTERNATIVES + 1]
            ]
            log.info(
                "resolver: fuzzy auto-accept %r -> %s (score %.2f; other candidates: %s)",
                library_name,
                top.library_id,
                scored[0][1],
                [r.library_id for r, _ in scored[1 : _MAX_ALTERNATIVES + 1]],
            )
            return self._found(top, alternatives=rest)

        if scored:
            log.info(
                "resolver: no exact match for %r; closest candidates: %s",
                library_name,
                [(r.library_id, s) for r, s in scored[:_MAX_ALTERNATIVES]],
            )
            return ResolutionResult(
                found=False,
                message=(
                    f"no exact match for '{library_name}'; "
                    "closest candidates below"
                ),
                hint="supply docs_url to register a new library",
                alternatives=[
                    Alternative(library_id=r.library_id, name=r.name, score=s)
                    for r, s in scored[:_MAX_ALTERNATIVES]
                ],
            )

        # 4. Unknown.
        log.info("resolver: unknown library %r (ecosystem=%s)", library_name, ecosystem)
        return ResolutionResult(
            found=False,
            message="unknown library",
            hint="supply docs_url to register",
            alternatives=[],
        )

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _found(
        record: LibraryRecord, alternatives: list[Alternative]
    ) -> ResolutionResult:
        return ResolutionResult(
            found=True,
            library_id=record.library_id,
            name=record.name,
            versions=list(record.versions),
            latest=record.latest,
            alternatives=alternatives,
        )

    def _fuzzy(self, query: str) -> list[tuple[LibraryRecord, float]]:
        """BM25-lite over library names/aliases/descriptions.

        Documents are the resolver rows; fields are weighted
        (name 3x, aliases 2x, description 1x) and scored with a BM25-style
        idf * tf-saturation term. Query-token substring hits on the name earn
        a small bonus so 'tf' still finds tensorflow.
        """
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        query_counts = Counter(query_tokens)
        n_docs = len(self._records)
        if n_docs == 0:
            return []
        query_trigrams = _trigrams(query)

        # Field texts per record, with weights.
        weights = {"name": 3.0, "aliases": 2.0, "description": 1.0}
        per_field_tokens: dict[str, dict[str, list[str]]] = {}
        for record in self._records:
            per_field_tokens[record.library_id] = {
                "name": _tokens(record.name),
                "aliases": _tokens(" ".join(record.aliases)),
                "description": _tokens(record.description),
            }

        # Document frequencies across all fields.
        df: Counter[str] = Counter()
        for record in self._records:
            seen = set()
            for field_toks in per_field_tokens[record.library_id].values():
                seen.update(field_toks)
            df.update(seen)

        def idf(term: str) -> float:
            return math.log(1 + (n_docs - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))

        k1, b = 1.2, 0.75
        avg_len = (
            sum(
                sum(len(t) for t in per_field_tokens[r.library_id].values())
                for r in self._records
            )
            / n_docs
        ) or 1.0

        scored: list[tuple[LibraryRecord, float]] = []
        for record in self._records:
            fields = per_field_tokens[record.library_id]
            score = 0.0
            for term, qf in query_counts.items():
                term_idf = idf(term)
                for field_name, field_toks in fields.items():
                    tf = field_toks.count(term)
                    if not tf:
                        continue
                    field_len = len(field_toks) or 1
                    denom = tf + k1 * (1 - b + b * field_len / avg_len)
                    score += (
                        weights[field_name]
                        * term_idf
                        * (tf * (k1 + 1) / denom)
                        * qf
                    )
            # Substring bonus: short queries like 'tf' matching inside the name.
            norm_query = _normalize(query)
            if norm_query and norm_query in _normalize(record.name):
                score += 1.5
            # Trigram tier: typo tolerance on name/aliases ('tensrflow').
            name_trigrams = [_trigrams(record.name)]
            name_trigrams.extend(_trigrams(a) for a in record.aliases)
            score += _TRIGRAM_WEIGHT * max(
                _jaccard(query_trigrams, t) for t in name_trigrams
            )
            if score >= _FUZZY_MIN_SCORE:
                scored.append((record, round(score, 3)))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored
