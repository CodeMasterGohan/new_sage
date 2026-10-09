"""Deletion helper tests (spec §12 pruning).

Uses a fake Milvus client — no live Milvus required. Validates the
filter expressions, ID-contract fail-closed behavior, and the plumbing
(collection name, filter kwarg, reported count).
"""

from __future__ import annotations

import pytest

from .. import deletion
from ..schema import COLLECTION_NAME


class FakeClient:
    def __init__(self, delete_count: int = 7):
        self.calls: list[dict] = []
        self._count = delete_count

    def delete(self, collection_name, filter):  # noqa: A002
        self.calls.append({"collection_name": collection_name, "filter": filter})
        return {"delete_count": self._count}


def test_delete_expr_library():
    assert (
        deletion.build_delete_expr_library("react")
        == 'library_id == "react"'
    )


def test_delete_expr_version():
    assert (
        deletion.build_delete_expr_version("react", "v18-3-0")
        == 'library_id == "react" and version_id == "v18-3-0"'
    )


@pytest.mark.parametrize(
    "bad",
    ["", "has space", 'quote"in', "back\\slash", "semi;colon", "x" * 65, None, 123],
)
def test_id_contract_rejects(bad):
    with pytest.raises(ValueError, match="invalid library_id"):
        deletion.build_delete_expr_library(bad)
    with pytest.raises(ValueError, match="invalid (library_id|version_id)"):
        deletion.build_delete_expr_version(bad, "v1")


def test_delete_library_issues_scoped_delete():
    client = FakeClient(delete_count=12)
    result = deletion.delete_library(client, "pytorch")
    assert client.calls == [
        {"collection_name": COLLECTION_NAME, "filter": 'library_id == "pytorch"'}
    ]
    assert result.library_id == "pytorch"
    assert result.version_id is None
    assert result.collection == COLLECTION_NAME
    assert result.delete_count == 12
    assert result.expr == 'library_id == "pytorch"'


def test_delete_version_issues_scoped_delete():
    client = FakeClient(delete_count=3)
    result = deletion.delete_version(client, "pytorch", "v2-4")
    assert client.calls == [
        {
            "collection_name": COLLECTION_NAME,
            "filter": 'library_id == "pytorch" and version_id == "v2-4"',
        }
    ]
    assert result.version_id == "v2-4"
    assert result.delete_count == 3


def test_delete_invalid_id_never_touches_client():
    client = FakeClient()
    with pytest.raises(ValueError):
        deletion.delete_library(client, "bad id")
    with pytest.raises(ValueError):
        deletion.delete_version(client, "react", "bad;version")
    assert client.calls == []


def test_delete_custom_collection_name():
    client = FakeClient()
    result = deletion.delete_version(client, "react", "v18", collection="sage_chunks_v2")
    assert client.calls[0]["collection_name"] == "sage_chunks_v2"
    assert result.collection == "sage_chunks_v2"


def test_version_id_accepts_dotted_version_strings():
    """Wave 2: version_id is the version STRING ("19.0.0") — dots allowed."""
    assert (
        deletion.build_delete_expr_version("react", "19.0.0")
        == 'library_id == "react" and version_id == "19.0.0"'
    )
    # library slugs stay dot-free.
    with pytest.raises(ValueError, match="invalid library_id"):
        deletion.build_delete_expr_library("re.act")
