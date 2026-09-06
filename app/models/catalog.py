"""Relational catalog models for Atlas logical assets and their renditions."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class LogicalAsset(Base):
    """A stable Atlas asset identity, independent of storage locations."""

    __tablename__ = "logical_assets"
    __table_args__ = (
        UniqueConstraint("producer", "source_movie_id", "producer_asset_id", name="uq_logical_assets_producer_source_asset"),
        CheckConstraint("catalog_scope IN ('title', 'brand', 'general')", name="ck_logical_assets_catalog_scope"),
        CheckConstraint("title_type IS NULL OR title_type IN ('movie', 'series')", name="ck_logical_assets_title_type"),
        CheckConstraint(
            "(catalog_scope = 'title' AND title_id IS NOT NULL AND title_type IS NOT NULL AND brand_id IS NULL) OR "
            "(catalog_scope = 'brand' AND brand_id IS NOT NULL AND title_id IS NULL AND title_type IS NULL) OR "
            "(catalog_scope = 'general' AND title_id IS NULL AND title_type IS NULL AND brand_id IS NULL)",
            name="ck_logical_assets_catalog_scope_fields",
        ),
        Index("ix_logical_assets_catalog_scope_title_id", "catalog_scope", "title_id"),
        Index("ix_logical_assets_catalog_scope_brand_id", "catalog_scope", "brand_id"),
    )

    asset_uid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    producer: Mapped[str] = mapped_column(String, nullable=False)
    producer_asset_id: Mapped[str] = mapped_column(String, nullable=False)
    source_movie_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active", server_default=text("'active'"))
    catalog_scope: Mapped[str] = mapped_column(String, nullable=False)
    title_id: Mapped[str | None] = mapped_column(String)
    title_type: Mapped[str | None] = mapped_column(String)
    brand_id: Mapped[str | None] = mapped_column(String)
    editorial_decision: Mapped[str | None] = mapped_column(String)
    editorial_status: Mapped[str | None] = mapped_column(String)
    source_movie_sha256: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    renditions: Mapped[list["AssetRendition"]] = relationship(back_populates="asset", cascade="all, delete-orphan")
    semantics: Mapped["AssetSemantics | None"] = relationship(back_populates="asset", cascade="all, delete-orphan", uselist=False)
    ingest_record: Mapped["IngestRecord | None"] = relationship(back_populates="asset", cascade="all, delete-orphan", uselist=False)


class AssetRendition(Base):
    """A physical horizontal or vertical representation of one logical asset."""

    __tablename__ = "asset_renditions"
    __table_args__ = (
        UniqueConstraint("asset_uid", "kind", name="uq_asset_renditions_asset_kind"),
        CheckConstraint("kind IN ('horizontal', 'vertical')", name="ck_asset_renditions_kind"),
        CheckConstraint("char_length(sha256) = 64", name="ck_asset_renditions_sha256_length"),
        CheckConstraint("thumbnail_sha256 IS NULL OR char_length(thumbnail_sha256) = 64", name="ck_asset_renditions_thumbnail_sha256_length"),
    )

    rendition_uid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_uid: Mapped[uuid.UUID] = mapped_column(ForeignKey("logical_assets.asset_uid"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    thumbnail_uri: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    thumbnail_sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    thumbnail_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    orientation: Mapped[str | None] = mapped_column(String)
    aspect_ratio: Mapped[str | None] = mapped_column(String)
    technical_validated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    semantic_validated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    semantic_overrides: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    asset: Mapped[LogicalAsset] = relationship(back_populates="renditions")


class AssetSemantics(Base):
    """Producer semantic and provenance metadata for one logical asset."""

    __tablename__ = "asset_semantics"

    asset_uid: Mapped[uuid.UUID] = mapped_column(ForeignKey("logical_assets.asset_uid"), primary_key=True)
    visual_summary: Mapped[str | None] = mapped_column(Text)
    subjects: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    objects: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    actions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    visible_emotions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    interactions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    setting: Mapped[str | None] = mapped_column(String)
    people_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    relationships_json: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    keywords: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    search_terms: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    negative_use_cases: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    standalone_meaning: Mapped[str | None] = mapped_column(Text)
    reusable_broll: Mapped[bool | None] = mapped_column(Boolean)
    action_or_moment_complete: Mapped[bool | None] = mapped_column(Boolean)
    narrative_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    editorial_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    search_text: Mapped[str | None] = mapped_column(Text)
    embedding_text: Mapped[str | None] = mapped_column(Text)
    raw_producer_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    asset: Mapped[LogicalAsset] = relationship(back_populates="semantics")


class IngestRecord(Base):
    """Durable current handoff state for an idempotent producer asset."""

    __tablename__ = "ingest_records"
    __table_args__ = (
        UniqueConstraint("producer", "source_movie_id", "producer_asset_id", name="uq_ingest_records_producer_source_asset"),
        UniqueConstraint("asset_uid", name="uq_ingest_records_asset_uid"),
        CheckConstraint(
            "state IN ('discovered', 'validated', 'copying_to_final', 'final_verified', 'cataloged', "
            "'cleanup_pending', 'complete', 'failed')",
            name="ck_ingest_records_state",
        ),
    )

    ingest_uid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    asset_uid: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("logical_assets.asset_uid"))
    producer: Mapped[str] = mapped_column(String, nullable=False)
    source_movie_id: Mapped[str] = mapped_column(String, nullable=False)
    producer_asset_id: Mapped[str] = mapped_column(String, nullable=False)
    source_base_uri: Mapped[str] = mapped_column(Text, nullable=False)
    package_basename: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    destination_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    catalog_committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    observed_package_fingerprint: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    asset: Mapped[LogicalAsset | None] = relationship(back_populates="ingest_record")
