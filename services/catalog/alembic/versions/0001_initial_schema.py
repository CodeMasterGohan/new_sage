"""Initial New_sage catalog schema (spec §4).

Revision ID: 0001_initial_schema
Revises: None

Tables: library, library_version, page, chunk, crawl_job.
No tenancy columns — New_sage is single-user (spec §13).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "library",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  default=uuid.uuid4),
        sa.Column("name", sa.Text(), unique=True, nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), nullable=False,
                  server_default="'{}'"),
        sa.Column("ecosystem", sa.String(64), nullable=True),
        sa.Column("docs_url", sa.Text(), nullable=True),
        sa.Column("repo_url", sa.Text(), nullable=True),
        sa.Column("resolver_notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )

    op.create_table(
        "library_version",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  default=uuid.uuid4),
        sa.Column("library_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("library.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("is_latest", sa.Boolean(), nullable=False,
                  server_default="false"),
        sa.Column("crawled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False,
                  server_default="pending"),
        sa.Column("refresh_after_days", sa.Integer(), nullable=False,
                  server_default="30"),
        sa.UniqueConstraint("library_id", "version", name="uq_library_version"),
    )
    op.create_index(
        "ix_library_version_library_id", "library_version", ["library_id"]
    )

    op.create_table(
        "page",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  default=uuid.uuid4),
        sa.Column("library_version_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("library_version.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("section_path", sa.Text(), nullable=True),
        sa.Column("raw_snapshot_key", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_page_library_version_id", "page", ["library_version_id"]
    )

    op.create_table(
        "chunk",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  default=uuid.uuid4),
        sa.Column("page_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("page.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("headings", postgresql.ARRAY(sa.Text()), nullable=False,
                  server_default="'{}'"),
    )
    op.create_index("ix_chunk_page_id", "chunk", ["page_id"])

    op.create_table(
        "crawl_job",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  default=uuid.uuid4),
        sa.Column("library_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("library.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("version", sa.String(128), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False,
                  server_default="queued"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_crawl_job_library_id", "crawl_job", ["library_id"])
    op.create_index("ix_crawl_job_status", "crawl_job", ["status"])


def downgrade() -> None:
    op.drop_index("ix_crawl_job_status", table_name="crawl_job")
    op.drop_index("ix_crawl_job_library_id", table_name="crawl_job")
    op.drop_table("crawl_job")
    op.drop_index("ix_chunk_page_id", table_name="chunk")
    op.drop_table("chunk")
    op.drop_index("ix_page_library_version_id", table_name="page")
    op.drop_table("page")
    op.drop_index("ix_library_version_library_id", table_name="library_version")
    op.drop_table("library_version")
    op.drop_table("library")
