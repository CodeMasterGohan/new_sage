"""Test for Alembic migration 0002_commit_sha (spec §9.3.1).

Run from the build root with the crawler venv (sqlalchemy 2.0.x, offline):
    services/crawler/.venv/bin/python -m pytest services/catalog/tests/test_migration_0002.py -q

Alembic is not installed in this offline environment (and cannot be
installed without network), so the test executes the migration module's
``upgrade()``/``downgrade()`` directly against SQLite with a minimal
``alembic.op`` shim. The shim emits the same DDL text alembic would for
these two operations (``ALTER TABLE ... ADD/DROP COLUMN``, rendered from
the migration's own ``sa.Column`` object), so the test genuinely validates
that the migration applies cleanly on SQLite and that the new
``commit_sha`` ORM field round-trips.

Scope note: only 0002 is exercised here. 0001_initial_schema is
Postgres-only by design (``postgresql.ARRAY`` does not compile on SQLite);
the catalog's SQLite suites cover the schema via ``Base.metadata.create_all``.
"""

import importlib.util
import sys
import types

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from services.catalog.models import Base, Library, LibraryVersion

MIGRATION_PATH = (
    "services/catalog/alembic/versions/0002_commit_sha.py"
)


class _OpShim:
    """Minimal ``alembic.op`` stand-in for offline migration testing.

    Implements exactly the two operations 0002_commit_sha uses, emitting
    the same DDL alembic would on SQLite (SQLite >= 3.35 supports
    DROP COLUMN).
    """

    def __init__(self, conn):
        self._conn = conn
        self._dialect = conn.dialect

    def add_column(self, table_name, column):
        coltype = column.type.compile(dialect=self._dialect)
        self._conn.execute(
            sa.text(f"ALTER TABLE {table_name} ADD COLUMN {column.name} {coltype}")
        )

    def drop_column(self, table_name, column_name):
        self._conn.execute(
            sa.text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}")
        )


def _load_migration(conn):
    """Import the migration module with the fake ``alembic`` injected."""
    fake_alembic = types.ModuleType("alembic")
    fake_alembic.op = _OpShim(conn)
    old = sys.modules.get("alembic")
    sys.modules["alembic"] = fake_alembic
    try:
        spec = importlib.util.spec_from_file_location(
            "sage_migration_0002_commit_sha", MIGRATION_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if old is None:
            sys.modules.pop("alembic", None)
        else:
            sys.modules["alembic"] = old


def _pre_migration_metadata():
    """Build metadata matching the schema *before* 0002 ran.

    Uses the current model columns minus ``commit_sha`` (models now carry
    the new field; the DB table built here must not have it yet).
    """
    md = sa.MetaData()
    Library.__table__.tometadata(md)
    lv_cols = [
        c.copy() for c in LibraryVersion.__table__.columns if c.name != "commit_sha"
    ]
    lv_cols.append(
        sa.UniqueConstraint("library_id", "version", name="uq_library_version")
    )
    sa.Table("library_version", md, *lv_cols)
    return md


@pytest.fixture()
def migrated_engine(tmp_path):
    """SQLite engine whose library_version table went through 0002's upgrade()."""
    engine = sa.create_engine(f"sqlite:///{tmp_path}/mig.db")
    md = _pre_migration_metadata()
    md.create_all(engine)
    with engine.begin() as conn:
        insp = sa.inspect(conn)
        assert "commit_sha" not in {c["name"] for c in insp.get_columns("library_version")}
        _load_migration(conn).upgrade()
    return engine


def test_0002_upgrade_adds_commit_sha_column(migrated_engine):
    engine = migrated_engine
    with engine.connect() as conn:
        cols = {c["name"]: c for c in sa.inspect(conn).get_columns("library_version")}
    assert "commit_sha" in cols
    assert cols["commit_sha"]["nullable"] is True


def test_0002_commit_sha_round_trips(migrated_engine):
    engine = migrated_engine
    sha = "9f2c4a7e" * 5  # 40-char fake git SHA
    with Session(engine) as s:
        lib = Library(name="fastapi", ecosystem="pypi")
        s.add(lib)
        s.flush()
        pinned = LibraryVersion(
            library_id=lib.id,
            version="0.115.0",
            source="repoclone",
            commit_sha=sha,
        )
        unpinned = LibraryVersion(
            library_id=lib.id, version="0.116.0", source="crawl"
        )
        s.add_all([pinned, unpinned])
        s.commit()
        assert s.get(LibraryVersion, pinned.id).commit_sha == sha
        assert s.get(LibraryVersion, unpinned.id).commit_sha is None


def test_0002_downgrade_drops_commit_sha_column(migrated_engine):
    engine = migrated_engine
    with engine.begin() as conn:
        _load_migration(conn).downgrade()
    with engine.connect() as conn:
        cols = {c["name"] for c in sa.inspect(conn).get_columns("library_version")}
    assert "commit_sha" not in cols


def test_0002_chain_metadata_matches_models(migrated_engine):
    """After upgrade, the DB table must accept everything the ORM model maps."""
    engine = migrated_engine
    with engine.connect() as conn:
        db_cols = {c["name"] for c in sa.inspect(conn).get_columns("library_version")}
    model_cols = {c.name for c in LibraryVersion.__table__.columns}
    assert model_cols <= db_cols, model_cols - db_cols
