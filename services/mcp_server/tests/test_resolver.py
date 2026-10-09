"""Resolver tests (spec §9.3): exact, alias, ecosystem disambiguation,
fuzzy BM25-over-names fallback, and the unknown shape."""

from __future__ import annotations

import pytest

from mcp_server.models import LibraryRecord, ResolutionResult


def _record(library_id: str, name: str, ecosystem: str, **kw) -> LibraryRecord:
    return LibraryRecord(
        library_id=library_id,
        name=name,
        aliases=kw.get("aliases", []),
        ecosystem=ecosystem,
        docs_url=kw.get("docs_url", f"https://example.invalid/{library_id}"),
        repo_url=kw.get("repo_url", f"https://example.invalid/{library_id}.git"),
        versions=kw.get("versions", ["1.0"]),
        latest=kw.get("latest", "1.0"),
        description=kw.get("description", ""),
    )


# -- exact / alias ----------------------------------------------------------


def test_exact_match_case_insensitive(resolver):
    for spelling in ("react", "React", "REACT", " react "):
        result = resolver.resolve(spelling)
        assert result.found is True
        assert result.library_id == "react"
        assert result.name == "React"
        assert result.latest == "19.2.0"
        assert "19.2.0" in result.versions


def test_exact_match_normalizes_separators(resolver):
    # 'react-dom' style separators collapse for exact matching.
    assert resolver.resolve("React").library_id == "react"


def test_alias_match(resolver):
    assert resolver.resolve("torch").library_id == "pytorch"
    assert resolver.resolve("tf").library_id == "tensorflow"
    assert resolver.resolve("psql").library_id == "postgres"
    assert resolver.resolve("golang").library_id == "go"
    assert resolver.resolve("reactjs").library_id == "react"


def test_exact_match_has_empty_alternatives(resolver):
    result = resolver.resolve("pandas")
    assert result.found is True
    assert result.alternatives == []


# -- ecosystem disambiguation ----------------------------------------------


def test_ecosystem_hint_disambiguates():
    recs = [
        _record("core-a", "core", "pypi"),
        _record("core-b", "core", "npm"),
    ]
    from mcp_server.resolver import LibraryResolver

    r = LibraryResolver(recs)
    ambiguous = r.resolve("core")
    assert ambiguous.found is False
    assert "ambiguous" in (ambiguous.message or "")
    assert {a.library_id for a in ambiguous.alternatives} == {"core-a", "core-b"}

    assert r.resolve("core", ecosystem="npm").library_id == "core-b"
    assert r.resolve("core", ecosystem="PyPI").library_id == "core-a"


def test_ecosystem_hint_harmless_on_unique(resolver):
    result = resolver.resolve("torch", ecosystem="pypi")
    assert result.found is True
    assert result.library_id == "pytorch"


# -- fuzzy fallback ----------------------------------------------------------


def test_fuzzy_strong_hit_auto_resolves(resolver):
    # 'deep learning framework' is distinctive in PyTorch's description.
    result = resolver.resolve("deep learning framework")
    assert result.found is True
    assert result.library_id == "pytorch"
    assert result.alternatives  # other candidates still surfaced


def test_fuzzy_topic_style_query(resolver):
    result = resolver.resolve("in-memory cache")
    assert result.found is True
    assert result.library_id == "redis"


def test_fuzzy_typo_returns_scored_alternatives(resolver):
    result = resolver.resolve("tensrflow")
    assert result.found is False
    assert "no exact match" in (result.message or "")
    top = result.alternatives[0]
    assert top.library_id == "tensorflow"
    assert top.score > 0
    scores = [a.score for a in result.alternatives]
    assert scores == sorted(scores, reverse=True)


# -- unknown -----------------------------------------------------------------


def test_unknown_library_shape(resolver):
    result = resolver.resolve("zzzqqqxxx")
    assert result == ResolutionResult(
        found=False,
        message="unknown library",
        hint="supply docs_url to register",
        alternatives=[],
    )
    dumped = result.model_dump(exclude_none=True)
    assert dumped == {
        "found": False,
        "versions": [],
        "alternatives": [],
        "message": "unknown library",
        "hint": "supply docs_url to register",
    }


def test_seed_table_has_fifteen_libraries(resolver):
    assert len(resolver._records) == 15


def test_get_by_library_id(resolver):
    record = resolver.get("react")
    assert record is not None
    assert record.name == "React"
    assert resolver.get("nope") is None
