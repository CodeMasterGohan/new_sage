"""Add commit_sha to library_version (repo-clone pinning, spec §9.3.1).

Revision ID: 0002_commit_sha
Revises: 0001_initial_schema

The crawler's repo-clone path records the pinned commit SHA in the crawl
job report; this migration gives that SHA a proper catalog column on the
version row (nullable — seed/crawl rows never set it).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_commit_sha"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "library_version",
        sa.Column("commit_sha", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("library_version", "commit_sha")
