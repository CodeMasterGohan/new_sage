"""Argument validation tests: library names, IDs, versions, topics, budgets."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_server.models import GetLibraryDocsArgs, ResolveLibraryIdArgs


@pytest.mark.parametrize("bad_name", ["", "   ", "x" * 201])
def test_resolve_rejects_blank_or_huge_name(bad_name):
    with pytest.raises(ValidationError):
        ResolveLibraryIdArgs(libraryName=bad_name)


def test_resolve_strips_and_accepts_name():
    args = ResolveLibraryIdArgs(libraryName="  react  ")
    assert args.libraryName == "react"
    assert args.ecosystem is None


def test_resolve_ecosystem_hint_validated():
    with pytest.raises(ValidationError):
        ResolveLibraryIdArgs(libraryName="requests", ecosystem="")
    with pytest.raises(ValidationError):
        ResolveLibraryIdArgs(libraryName="requests", ecosystem="x" * 65)
    ok = ResolveLibraryIdArgs(libraryName="requests", ecosystem=" pypi ")
    assert ok.ecosystem == "pypi"


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../etc/passwd",
        "/abs/path",
        "a/b",
        "a b",
        "",
        "x" * 129,
        "lib;DROP TABLE",
        "..",
        "-leading-dash",
    ],
)
def test_library_id_rejects_paths_and_junk(bad_id):
    with pytest.raises(ValidationError):
        GetLibraryDocsArgs(library_id=bad_id)


@pytest.mark.parametrize("good_id", ["react", "python-requests", "vue", "a" * 128])
def test_library_id_accepts_sane_ids(good_id):
    args = GetLibraryDocsArgs(library_id=f"  {good_id}  ")
    assert args.library_id == good_id


@pytest.mark.parametrize("bad_tokens", [0, -1, 100_001])
def test_tokens_bounds(bad_tokens):
    with pytest.raises(ValidationError):
        GetLibraryDocsArgs(library_id="react", tokens=bad_tokens)


def test_tokens_default():
    assert GetLibraryDocsArgs(library_id="react").tokens == 10000


@pytest.mark.parametrize("bad_version", ["", "   ", "v" * 65])
def test_version_validated(bad_version):
    with pytest.raises(ValidationError):
        GetLibraryDocsArgs(library_id="react", version=bad_version)


def test_topic_validated():
    with pytest.raises(ValidationError):
        GetLibraryDocsArgs(library_id="react", topic="   ")
    with pytest.raises(ValidationError):
        GetLibraryDocsArgs(library_id="react", topic="x" * 2001)
    ok = GetLibraryDocsArgs(library_id="react", topic="  hooks  ")
    assert ok.topic == "hooks"
