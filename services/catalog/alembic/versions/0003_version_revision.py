"""Add revision to library_version for immutable re-crawl revisions (spec §4).

Revision ID: 0003_version_revision
Revises: 0002_commit_sha

Re-crawls now INSERT a new ``library_version`` row
(``revision = max + 1``) instead of updating the row in place. The unique
constraint moves from (library_id, version) to
(library_id, version, revision). Existing rows are backfilled to
``revision=1`` via the column's server default.

Downgrade note: restoring the old UNIQUE(library_id, version) fails if
more than one revision exists for any (library, version) — prune
superseded revisions first.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_version_revision"
down_revision = "0002_commit_sha"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "library_version",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.drop_constraint("uq_library_version", "library_version", type_="unique")
    op.create_unique_constraint(
        "uq_library_version_revision",
        "library_version",
        ["library_id", "version", "revision"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_library_version_revision", "library_version", type_="unique"
    )
    op.create_unique_constraint(
        "uq_library_version", "library_version", ["library_id", "version"]
    )
    op.drop_column("library_version", "revision")
