"""DB-backed resolver seam (Wave 2): ``LibraryResolver.from_catalog``.

Builds a resolver from a real (SQLite) catalog and checks the
row -> LibraryRecord mapping. Skipped when SQLAlchemy is not installed —
the mcp-server image does not bundle it yet (see the deployment note in
``resolver.from_catalog``).
"""

from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("sqlalchemy")

BUILD_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
if BUILD_ROOT not in sys.path:
    sys.path.insert(0, BUILD_ROOT)

from mcp_server.resolver import LibraryResolver  # noqa: E402


def _seed_db(path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from services.catalog import CatalogRepository
    from services.catalog.models import Base

    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            repo = CatalogRepository(session)
            lib = repo.create_library(
                name="react",
                aliases=["reactjs"],
                ecosystem="npm",
                docs_url="https://react.dev",
                repo_url="https://github.com/facebook/react",
            )
            _, _ = repo.get_or_create_version(lib.id, "18.3.0", source="seed")
            v2, _ = repo.get_or_create_version(lib.id, "19.2.0", source="seed")
            repo.mark_version_ready(v2.id, make_latest=True)
            session.commit()
    finally:
        engine.dispose()


def test_from_catalog_maps_rows_to_records(tmp_path):
    db = tmp_path / "resolver.db"
    _seed_db(db)

    resolver = LibraryResolver.from_catalog(f"sqlite:///{db}")

    rec = resolver.get("react")
    assert rec is not None
    assert rec.library_id == "react"  # slug, not the UUID
    assert rec.name == "react"
    assert rec.ecosystem == "npm"
    assert rec.aliases == ["reactjs"]
    assert set(rec.versions) == {"18.3.0", "19.2.0"}
    assert rec.latest == "19.2.0"
    assert rec.docs_url == "https://react.dev"
    assert rec.repo_url == "https://github.com/facebook/react"

    # Alias tier still works on DB-backed records.
    resolved = resolver.resolve("reactjs")
    assert resolved.found is True
    assert resolved.library_id == "react"
