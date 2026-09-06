"""Initial Atlas catalog schema.

Revision ID: 20260906_0001
Revises:
Create Date: 2026-09-06
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260906_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "logical_assets",
        sa.Column("asset_uid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("producer", sa.String(), nullable=False),
        sa.Column("producer_asset_id", sa.String(), nullable=False),
        sa.Column("source_movie_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("catalog_scope", sa.String(), nullable=False),
        sa.Column("title_id", sa.String()),
        sa.Column("title_type", sa.String()),
        sa.Column("brand_id", sa.String()),
        sa.Column("editorial_decision", sa.String()),
        sa.Column("editorial_status", sa.String()),
        sa.Column("source_movie_sha256", sa.String()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("catalog_scope IN ('title', 'brand', 'general')", name="ck_logical_assets_catalog_scope"),
        sa.CheckConstraint("title_type IS NULL OR title_type IN ('movie', 'series')", name="ck_logical_assets_title_type"),
        sa.CheckConstraint(
            "(catalog_scope = 'title' AND title_id IS NOT NULL AND title_type IS NOT NULL AND brand_id IS NULL) OR "
            "(catalog_scope = 'brand' AND brand_id IS NOT NULL AND title_id IS NULL AND title_type IS NULL) OR "
            "(catalog_scope = 'general' AND title_id IS NULL AND title_type IS NULL AND brand_id IS NULL)",
            name="ck_logical_assets_catalog_scope_fields",
        ),
        sa.PrimaryKeyConstraint("asset_uid"),
        sa.UniqueConstraint("producer", "source_movie_id", "producer_asset_id", name="uq_logical_assets_producer_source_asset"),
    )
    op.create_index("ix_logical_assets_source_movie_id", "logical_assets", ["source_movie_id"])
    op.create_index("ix_logical_assets_catalog_scope_title_id", "logical_assets", ["catalog_scope", "title_id"])
    op.create_index("ix_logical_assets_catalog_scope_brand_id", "logical_assets", ["catalog_scope", "brand_id"])

    op.create_table(
        "asset_renditions",
        sa.Column("rendition_uid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_uid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("thumbnail_uri", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("thumbnail_sha256", sa.String(length=64)),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("thumbnail_size_bytes", sa.BigInteger()),
        sa.Column("mime_type", sa.String()),
        sa.Column("duration_seconds", sa.Float()),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("fps", sa.Float()),
        sa.Column("orientation", sa.String()),
        sa.Column("aspect_ratio", sa.String()),
        sa.Column("technical_validated", sa.Boolean(), nullable=False),
        sa.Column("semantic_validated", sa.Boolean(), nullable=False),
        sa.Column("semantic_overrides", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('horizontal', 'vertical')", name="ck_asset_renditions_kind"),
        sa.CheckConstraint("char_length(sha256) = 64", name="ck_asset_renditions_sha256_length"),
        sa.CheckConstraint("thumbnail_sha256 IS NULL OR char_length(thumbnail_sha256) = 64", name="ck_asset_renditions_thumbnail_sha256_length"),
        sa.ForeignKeyConstraint(["asset_uid"], ["logical_assets.asset_uid"]),
        sa.PrimaryKeyConstraint("rendition_uid"),
        sa.UniqueConstraint("asset_uid", "kind", name="uq_asset_renditions_asset_kind"),
    )

    op.create_table(
        "asset_semantics",
        sa.Column("asset_uid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("visual_summary", sa.Text()),
        sa.Column("subjects", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("objects", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("actions", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("visible_emotions", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("interactions", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("setting", sa.String()),
        sa.Column("people_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("relationships_json", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("keywords", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("search_terms", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("negative_use_cases", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("standalone_meaning", sa.Text()),
        sa.Column("reusable_broll", sa.Boolean()),
        sa.Column("action_or_moment_complete", sa.Boolean()),
        sa.Column("narrative_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("provenance_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("editorial_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("search_text", sa.Text()),
        sa.Column("embedding_text", sa.Text()),
        sa.Column("raw_producer_metadata", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["asset_uid"], ["logical_assets.asset_uid"]),
        sa.PrimaryKeyConstraint("asset_uid"),
    )

    op.create_table(
        "ingest_records",
        sa.Column("ingest_uid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_uid", postgresql.UUID(as_uuid=True)),
        sa.Column("producer", sa.String(), nullable=False),
        sa.Column("source_movie_id", sa.String(), nullable=False),
        sa.Column("producer_asset_id", sa.String(), nullable=False),
        sa.Column("source_base_uri", sa.Text(), nullable=False),
        sa.Column("package_basename", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("destination_verified_at", sa.DateTime(timezone=True)),
        sa.Column("catalog_committed_at", sa.DateTime(timezone=True)),
        sa.Column("source_released_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("observed_package_fingerprint", sa.String()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('discovered', 'validated', 'copying_to_final', 'final_verified', 'cataloged', "
            "'cleanup_pending', 'complete', 'failed')",
            name="ck_ingest_records_state",
        ),
        sa.ForeignKeyConstraint(["asset_uid"], ["logical_assets.asset_uid"]),
        sa.PrimaryKeyConstraint("ingest_uid"),
        sa.UniqueConstraint("asset_uid", name="uq_ingest_records_asset_uid"),
        sa.UniqueConstraint("producer", "source_movie_id", "producer_asset_id", name="uq_ingest_records_producer_source_asset"),
    )


def downgrade() -> None:
    op.drop_table("ingest_records")
    op.drop_table("asset_semantics")
    op.drop_table("asset_renditions")
    op.drop_index("ix_logical_assets_catalog_scope_brand_id", table_name="logical_assets")
    op.drop_index("ix_logical_assets_catalog_scope_title_id", table_name="logical_assets")
    op.drop_index("ix_logical_assets_source_movie_id", table_name="logical_assets")
    op.drop_table("logical_assets")
