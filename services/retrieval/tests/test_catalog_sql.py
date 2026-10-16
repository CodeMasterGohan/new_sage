"""PostgresLibraryCatalog contract tests (Wave 2 wiring).

The catalog agent owns the DDL (``services/catalog/models.py``): the
``library`` / ``library_version`` tables use UUID ``id`` primary keys —
there are NO ``library_id`` / ``version_id`` columns. These tests stub
``psycopg`` and verify the raw SQL in ``services/retrieval/catalog.py``
references the real columns, resolves slugs via the canonical name, and
returns the version STRING as the Milvus-facing ``version_id`` (matching
what the ingestion pipeline writes into ``sage_chunks``).
"""

from __future__ import annotations

import sys
import types

import pytest

from services.retrieval import catalog as catalog_mod
from services.retrieval.errors import VersionResolutionError

FAKE_UUID = "12345678-1234-5678-1234-567812345678"


class _FakeCursor:
    def __init__(self, script):
        self._script = script  # list of rows (or None) to return per fetchone
        self.executed: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))

    def fetchone(self):
        if not self._script:
            return None
        return self._script.pop(0)


class _FakeConn:
    def __init__(self, script):
        self.cursor_obj = _FakeCursor(script)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self.cursor_obj


def _install_fake_psycopg(monkeypatch, script):
    """Scripted rows: one entry per fetchone() call, in call order.

    The script is shared across connections — each ``_connect()`` in the
    catalog class opens a new connection, and rows are consumed in order.
    """
    shared = list(script)
    conns: list[_FakeConn] = []

    def connect(dsn):
        conn = _FakeConn(shared)
        conns.append(conn)
        return conn

    module = types.ModuleType("psycopg")
    module.connect = connect
    monkeypatch.setitem(sys.modules, "psycopg", module)
    return conns


@pytest.fixture()
def cat():
    return catalog_mod.PostgresLibraryCatalog("postgresql://fake")


def test_get_library_matches_on_canonical_name(monkeypatch, cat):
    conns = _install_fake_psycopg(
        monkeypatch,
        script=[(FAKE_UUID, "react", "npm", ["reactjs"])],
    )
    record = cat.get_library("react")
    assert record is not None
    # Milvus-facing library_id is the canonical slug, not the UUID.
    assert record.library_id == "react"
    assert record.name == "react"
    assert record.ecosystem == "npm"
    assert record.aliases == ("reactjs",)
    sql = conns[0].cursor_obj.executed[0][0]
    assert "FROM library" in sql
    assert "lower(name)" in sql
    assert "library_id" not in sql.split("FROM")[0]  # no phantom column


def test_get_library_falls_back_to_alias(monkeypatch, cat):
    conns = _install_fake_psycopg(
        monkeypatch,
        script=[None, (FAKE_UUID, "react", "npm", ["reactjs"])],
    )
    record = cat.get_library("reactjs")
    assert record is not None
    assert record.library_id == "react"
    assert len(conns[0].cursor_obj.executed) == 2
    assert "unnest(aliases)" in conns[0].cursor_obj.executed[1][0]


def test_get_library_unknown_returns_none(monkeypatch, cat):
    _install_fake_psycopg(monkeypatch, script=[None, None])
    assert cat.get_library("nope") is None


def test_resolve_version_exact_returns_version_string(monkeypatch, cat):
    conns = _install_fake_psycopg(
        monkeypatch,
        script=[
            (FAKE_UUID, "react", "npm", []),  # library uuid lookup
            ("19.0.0",),  # exact version hit
        ],
    )
    resolved = cat.resolve_version("react", "19.0.0")
    assert resolved.version == "19.0.0"
    # Milvus-facing version_id is the version string (what ingestion
    # writes), NOT the catalog's UUID primary key.
    assert resolved.version_id == "19.0.0"
    assert resolved.fallback is False
    # _library_uuid() opens its own connection; the version query ran on
    # the second one.
    version_sql = conns[1].cursor_obj.executed[0][0]
    assert "FROM library_version" in version_sql
    assert "version_id" not in version_sql  # no phantom column
    # Spec §4 revisions: only the active (newest READY) revision resolves.
    assert "status = 'ready'" in version_sql
    assert "ORDER BY revision DESC" in version_sql


def test_resolve_version_falls_back_to_latest(monkeypatch, cat):
    _install_fake_psycopg(
        monkeypatch,
        script=[
            (FAKE_UUID, "react", "npm", []),
            None,  # no exact hit
            ("19.2.0",),  # is_latest row
        ],
    )
    resolved = cat.resolve_version("react", "9.9.9")
    assert resolved.version == "19.2.0"
    assert resolved.version_id == "19.2.0"
    assert resolved.fallback is True


def test_resolve_version_none_requests_latest(monkeypatch, cat):
    conns = _install_fake_psycopg(
        monkeypatch,
        script=[
            (FAKE_UUID, "react", "npm", []),
            ("19.2.0",),
        ],
    )
    resolved = cat.resolve_version("react", None)
    assert resolved.version == "19.2.0"
    assert resolved.fallback is False
    assert "status = 'ready'" in conns[1].cursor_obj.executed[0][0]


def test_resolve_version_unknown_library_raises(monkeypatch, cat):
    _install_fake_psycopg(monkeypatch, script=[None, None])
    with pytest.raises(VersionResolutionError):
        cat.resolve_version("nope", "1.0.0")


def test_resolve_version_no_rows_raises(monkeypatch, cat):
    _install_fake_psycopg(
        monkeypatch,
        script=[
            (FAKE_UUID, "react", "npm", []),
            None,
            None,
        ],
    )
    with pytest.raises(VersionResolutionError):
        cat.resolve_version("react", "9.9.9")
