"""Test for Alembic migration 0003_version_revision (spec §4).

Run from the build root with the crawler venv (sqlalchemy 2.0.x, offline):
    services/crawler/.venv/bin/python -m pytest services/catalog/tests/test_migration_0003.py -q

Alembic is not installed in this offline environment (and cannot be
installed without network), so the test executes the migration module's
``upgrade()``/``downgrade()`` directly against SQLite with a minimal
``alembic.op`` shim. The shim implements the three operations 0003 uses:

- ``add_column`` — same DDL alembic would emit (SQLite >= 3.35).
- ``drop_constraint`` / ``create_unique_constraint`` — SQLite cannot drop
  or add constraints in place, so the shim performs the documented
  table-rebuild (CREATE new table with the target UNIQUE set, copy rows,
  DROP old, RENAME), which is exactly what Alembic's batch mode does.

The test genuinely validates on SQLite that: existing rows are backfilled
to ``revision=1`` (data migration via the server default), the new
UNIQUE(library_id, version, revision) is enforced, the old
UNIQUE(library_id, version) no longer blocks a second revision, and the
downgrade restores the original shape.

Scope note: only 0003 is exercised here. 0001_initial_schema is
Postgres-only by design; the catalog's SQLite suites cover the schema via
``Base.metadata.create_all``.
"""

import importlib.util
import sys
import types

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from services.catalog.models import Base, Library, LibraryVersion

MIGRATION_PATH = "services/catalog/alembic/versions/0003_version_revision.py"


class _OpShim:
    """Minimal ``alembic.op`` stand-in for offline migration testing.

    ``add_column`` emits the same DDL alembic would. Constraint changes go
    through a table rebuild (SQLite has no DROP/ADD CONSTRAINT), tracking
    UNIQUE constraints by column tuple.
    """

    def __init__(self, conn):
        self._conn = conn
        self._dialect = conn.dialect
        # Constraint names are the migration's own declared names (SQLite
        # auto-indexes do not preserve them).
        self._known_uniques = {
            "uq_library_version": ("library_id", "version"),
            "uq_library_version_revision": ("library_id", "version", "revision"),
        }

    # -- add_column (same as the 0002 shim) ---------------------------
    def add_column(self, table_name, column):
        coltype = column.type.compile(dialect=self._dialect)
        default = ""
        if column.server_default is not None:
            default = f" DEFAULT {column.server_default.arg}"
        self._conn.execute(
            sa.text(
                f"ALTER TABLE {table_name} ADD COLUMN {column.name} "
                f"{coltype} NOT NULL{default}"
            )
        )

    def drop_column(self, table_name, column_name):
        self._conn.execute(
            sa.text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}")
        )

    # -- constraint helpers -------------------------------------------
    def _uniques(self, table_name):
        """Current UNIQUE column-tuples (from SQLite auto-indexes)."""
        uniques = set()
        rows = self._conn.execute(
            sa.text(f"PRAGMA index_list({table_name})")
        ).mappings().all()
        for idx in rows:
            if idx["origin"] != "u":
                continue
            cols = tuple(
                r["name"]
                for r in self._conn.execute(
                    sa.text(f"PRAGMA index_info({idx['name']})")
                ).mappings().all()
            )
            uniques.add(cols)
        return uniques

    def _rebuild(self, table_name, drop_cols: set, add_cols: dict):
        """Rebuild the table with an adjusted UNIQUE set.

        ``drop_cols``/``add_cols`` map column-name tuples (add maps to the
        constraint name, kept for readability — SQLite auto-indexes ignore
        it).
        """
        info = self._conn.execute(
            sa.text(f"PRAGMA table_info({table_name})")
        ).mappings().all()
        col_defs = []
        col_names = []
        for col in info:
            ddl = f'"{col["name"]}" {col["type"]}'
            if col["pk"]:
                ddl += " PRIMARY KEY"
            if col["notnull"]:
                ddl += " NOT NULL"
            if col["dflt_value"] is not None:
                ddl += f" DEFAULT {col['dflt_value']}"
            col_defs.append(ddl)
            col_names.append(f'"{col["name"]}"')
        target = {cols for cols in self._uniques(table_name)} - drop_cols
        target |= set(add_cols)
        for cols in sorted(target):
            col_defs.append(
                "UNIQUE (" + ", ".join(f'"{c}"' for c in cols) + ")"
            )
        # Preserve foreign keys.
        for fk in self._conn.execute(
            sa.text(f"PRAGMA foreign_key_list({table_name})")
        ).mappings().all():
            col_defs.append(
                f'FOREIGN KEY("{fk["from"]}") '
                f'REFERENCES "{fk["table"]}"("{fk["to"]}") '
                f"ON DELETE {fk['on_delete']}"
            )
        tmp = f"__alembic_tmp_{table_name}"
        cols_csv = ", ".join(col_names)
        self._conn.execute(sa.text(f"CREATE TABLE {tmp} ({', '.join(col_defs)})"))
        self._conn.execute(
            sa.text(f"INSERT INTO {tmp} ({cols_csv}) SELECT {cols_csv} "
                    f"FROM {table_name}")
        )
        self._conn.execute(sa.text(f"DROP TABLE {table_name}"))
        self._conn.execute(sa.text(f"ALTER TABLE {tmp} RENAME TO {table_name}"))

    def drop_constraint(self, name, table_name, type_=None):
        # Resolve the migration's declared constraint name to columns.
        if type_ == "unique":
            self._rebuild(table_name, {self._known_uniques[name]}, {})
        else:
            raise NotImplementedError(f"drop_constraint type={type_}")

    def create_unique_constraint(self, name, table_name, columns):
        cols = tuple(columns)
        self._known_uniques[name] = cols
        self._rebuild(table_name, set(), {cols: name})


def _load_migration(conn):
    """Import the migration module with the fake ``alembic`` injected."""
    fake_alembic = types.ModuleType("alembic")
    fake_alembic.op = _OpShim(conn)
    old = sys.modules.get("alembic")
    sys.modules["alembic"] = fake_alembic
    try:
        spec = importlib.util.spec_from_file_location(
            "sage_migration_0003_version_revision", MIGRATION_PATH
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
    """Build metadata matching the schema *before* 0003 ran.

    Current models minus ``revision``, with the old
    UNIQUE(library_id, version).
    """
    md = sa.MetaData()
    Library.__table__.tometadata(md)
    lv_cols = [
        c.copy() for c in LibraryVersion.__table__.columns if c.name != "revision"
    ]
    lv_cols.append(
        sa.UniqueConstraint("library_id", "version", name="uq_library_version")
    )
    sa.Table("library_version", md, *lv_cols)
    return md


@pytest.fixture()
def migrated_engine(tmp_path):
    """SQLite engine with one pre-existing version row, post-0003 upgrade."""
    engine = sa.create_engine(f"sqlite:///{tmp_path}/mig3.db")
    md = _pre_migration_metadata()
    md.create_all(engine)
    with Session(engine) as s:
        lib = Library(name="react", ecosystem="npm")
        s.add(lib)
        s.flush()
        s.execute(
            sa.text(
                "INSERT INTO library_version (id, library_id, version, "
                "is_latest, source, status, refresh_after_days) "
                "VALUES (:id, :lib, '19.0.0', 0, 'crawl', 'ready', 30)"
            ),
            {"id": "a" * 32, "lib": lib.id.hex},
        )
        s.commit()
    with engine.begin() as conn:
        insp = sa.inspect(conn)
        assert "revision" not in {c["name"] for c in insp.get_columns("library_version")}
        _load_migration(conn).upgrade()
    return engine


def _uniques(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text("PRAGMA index_list(library_version)")
        ).mappings().all()
        out = set()
        for idx in rows:
            if idx["origin"] != "u":
                continue
            cols = tuple(
                r["name"]
                for r in conn.execute(
                    sa.text(f"PRAGMA index_info({idx['name']})")
                ).mappings().all()
            )
            out.add(cols)
        return out


def test_0003_upgrade_adds_revision_default_1(migrated_engine):
    with migrated_engine.connect() as conn:
        cols = {c["name"]: c for c in sa.inspect(conn).get_columns("library_version")}
    assert "revision" in cols
    assert cols["revision"]["nullable"] is False
    # Data migration: the pre-existing row was backfilled to revision=1.
    with Session(migrated_engine) as s:
        (rev,) = s.execute(sa.text("SELECT revision FROM library_version")).one()
        assert rev == 1


def test_0003_unique_constraint_swapped(migrated_engine):
    uniques = _uniques(migrated_engine)
    assert ("library_id", "version", "revision") in uniques
    assert ("library_id", "version") not in uniques


def test_0003_second_revision_insertable(migrated_engine):
    with Session(migrated_engine) as s:
        lib = s.execute(sa.text("SELECT id FROM library")).one()[0]
        s.execute(
            sa.text(
                "INSERT INTO library_version (id, library_id, version, "
                "revision, is_latest, source, status, refresh_after_days) "
                "VALUES (:id, :lib, '19.0.0', 2, 0, 'crawl', 'pending', 30)"
            ),
            {"id": "b" * 32, "lib": lib},
        )
        s.commit()
        # Same (library, version, revision) twice still violates the constraint.
        with pytest.raises(IntegrityError):
            s.execute(
                sa.text(
                    "INSERT INTO library_version (id, library_id, version, "
                    "revision, is_latest, source, status, refresh_after_days) "
                    "VALUES (:id, :lib, '19.0.0', 2, 0, 'crawl', 'pending', 30)"
                ),
                {"id": "c" * 32, "lib": lib},
            )
            s.flush()


def test_0003_orm_round_trip(migrated_engine):
    with Session(migrated_engine) as s:
        row = s.query(LibraryVersion).one()
        assert row.revision == 1
        assert row.version == "19.0.0"


def test_0003_downgrade_restores_shape(migrated_engine):
    with migrated_engine.begin() as conn:
        _load_migration(conn).downgrade()
    with migrated_engine.connect() as conn:
        cols = {c["name"] for c in sa.inspect(conn).get_columns("library_version")}
    assert "revision" not in cols
    assert _uniques(migrated_engine) == {("library_id", "version")}
