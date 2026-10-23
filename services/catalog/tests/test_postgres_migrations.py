"""Real migration regression; requires a disposable SAGE_TEST_DATABASE_URL."""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def test_fresh_postgres_migrations_and_array_defaults(monkeypatch):
    base_url = os.environ.get('SAGE_TEST_DATABASE_URL')
    if not base_url:
        pytest.skip('requires disposable SAGE_TEST_DATABASE_URL')
    schema = 'sage_migration_' + uuid.uuid4().hex
    admin = create_engine(base_url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA {schema}'))
    scoped = make_url(base_url).update_query_dict({'options': f'-csearch_path={schema}'})
    monkeypatch.setenv('DATABASE_URL', scoped.render_as_string(hide_password=False))
    root = Path(__file__).resolve().parents[3]
    engine = create_engine(scoped)
    try:
        # The CLI configures process-wide logging; isolate it from pytest.
        for _ in range(2):
            subprocess.run(
                [sys.executable, '-m', 'alembic', 'upgrade', 'head'],
                cwd=root / 'services/catalog', check=True, capture_output=True, text=True,
            )
        with engine.begin() as conn:
            revision = conn.scalar(text('SELECT version_num FROM alembic_version'))
            assert revision == '0003_version_revision'
            lib, version, page, chunk = (uuid.uuid4() for _ in range(4))
            aliases = conn.scalar(text(
                "INSERT INTO library (id,name) VALUES (:id,'demo') RETURNING aliases"
            ), {'id': lib})
            assert aliases == []
            conn.execute(text(
                "INSERT INTO library_version (id,library_id,version,source) "
                "VALUES (:id,:lib,'1','manual')"
            ), {'id': version, 'lib': lib})
            conn.execute(text(
                'INSERT INTO page (id,library_version_id) VALUES (:id,:version)'
            ), {'id': page, 'version': version})
            headings = conn.scalar(text(
                "INSERT INTO chunk (id,page_id,ordinal,text) "
                "VALUES (:id,:page,0,'hello') RETURNING headings"
            ), {'id': chunk, 'page': page})
            assert headings == []
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()
