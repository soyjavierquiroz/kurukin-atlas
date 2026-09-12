"""Generalize Atlas catalog identity from movies to opaque producer source keys.

Revision ID: 20260912_0002
Revises: 20260906_0001
Create Date: 2026-09-12
"""

from alembic import op
import sqlalchemy as sa


revision = "20260912_0002"
down_revision = "20260906_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename preserves every existing key value.  Existing Atlas producers were movies.
    op.alter_column("logical_assets", "source_movie_id", new_column_name="source_key")
    op.alter_column("ingest_records", "source_movie_id", new_column_name="source_key")
    op.add_column("logical_assets", sa.Column("source_kind", sa.String(), nullable=False, server_default=sa.text("'movie'")))
    op.add_column("ingest_records", sa.Column("source_kind", sa.String(), nullable=False, server_default=sa.text("'movie'")))
    op.alter_column("logical_assets", "source_kind", server_default=None)
    op.alter_column("ingest_records", "source_kind", server_default=None)
    op.alter_column("asset_renditions", "thumbnail_uri", nullable=True)
    op.drop_index("ix_logical_assets_source_movie_id", table_name="logical_assets")
    op.create_index("ix_logical_assets_source_key", "logical_assets", ["source_key"])


def downgrade() -> None:
    # PostgreSQL naturally rejects this if any thumbnail_uri is NULL; do not invent values.
    op.alter_column("asset_renditions", "thumbnail_uri", nullable=False)
    op.drop_index("ix_logical_assets_source_key", table_name="logical_assets")
    op.create_index("ix_logical_assets_source_movie_id", "logical_assets", ["source_key"])
    op.drop_column("ingest_records", "source_kind")
    op.drop_column("logical_assets", "source_kind")
    op.alter_column("ingest_records", "source_key", new_column_name="source_movie_id")
    op.alter_column("logical_assets", "source_key", new_column_name="source_movie_id")
