"""Metadata-only checks for the initial Atlas catalog schema."""

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base
from app.models import AssetRendition, AssetSemantics, IngestRecord, LogicalAsset


def _unique_columns(table: object) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints  # type: ignore[union-attr]
        if isinstance(constraint, UniqueConstraint)
    }


def _check_names(table: object) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints  # type: ignore[union-attr]
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }


def test_catalog_tables_are_registered_in_metadata() -> None:
    assert {"logical_assets", "asset_renditions", "asset_semantics", "ingest_records"} <= set(Base.metadata.tables)


def test_logical_asset_identity_and_scope_constraints() -> None:
    table = LogicalAsset.__table__

    assert ("producer", "source_key", "producer_asset_id") in _unique_columns(table)
    assert {"ck_logical_assets_catalog_scope", "ck_logical_assets_catalog_scope_fields", "ck_logical_assets_title_type"} <= _check_names(table)
    assert {index.name for index in table.indexes} >= {
        "ix_logical_assets_source_key",
        "ix_logical_assets_catalog_scope_title_id",
        "ix_logical_assets_catalog_scope_brand_id",
    }
    assert table.c.source_kind.nullable is False


def test_rendition_identity_and_kind_constraints() -> None:
    table = AssetRendition.__table__

    assert ("asset_uid", "kind") in _unique_columns(table)
    assert "ck_asset_renditions_kind" in _check_names(table)
    assert not any(constraint.columns.keys() == ["storage_uri"] for constraint in table.constraints if isinstance(constraint, UniqueConstraint))
    assert table.c.storage_uri.unique is not True
    assert table.c.thumbnail_uri.nullable is True


def test_semantics_uses_jsonb_and_text_not_a_vector_column() -> None:
    table = AssetSemantics.__table__

    assert isinstance(table.c.raw_producer_metadata.type, JSONB)
    assert str(table.c.embedding_text.type) == "TEXT"
    assert "embedding" not in table.c
    assert not any("vector" in type(column.type).__name__.lower() for column in table.columns)


def test_ingest_natural_key_and_state_constraint() -> None:
    table = IngestRecord.__table__

    assert ("producer", "source_key", "producer_asset_id") in _unique_columns(table)
    assert "ck_ingest_records_state" in _check_names(table)
    assert table.c.source_kind.nullable is False


def test_alembic_script_directory_loads_head_revision() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)

    assert script.get_current_head() == "20260912_0002"
