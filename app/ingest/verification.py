"""Read-only verification of a committed catalog handoff in a fresh session."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest.catalog import CatalogIngestRequest, rendition_semantic_overrides
from app.models import AssetRendition, AssetSemantics, IngestRecord, LogicalAsset


class CatalogVerificationError(RuntimeError):
    """Committed catalog data does not match the verified package expectation."""


def _check_fields(row, expected: dict, label: str) -> None:
    for field, value in expected.items():
        if getattr(row, field) != value:
            raise CatalogVerificationError(f"{label}.{field} mismatch")


def _one(rows, label: str):
    if len(rows) != 1:
        raise CatalogVerificationError(f"expected exactly one {label} row; found {len(rows)}")
    return rows[0]


def verify_cataloged_asset(session: Session, expectation: CatalogIngestRequest) -> str:
    """Verify persisted identity, custody, semantics and state; return observed state.

    The caller must provide a new session after the catalog transaction commits.
    This helper never writes, commits, or repairs catalog data.
    """
    asset = expectation.normalized_asset
    placement = expectation.placement

    def natural_rows(model):
        return list(session.scalars(select(model).where(
            model.producer == asset.producer,
            model.source_movie_id == asset.source_movie_id,
            model.producer_asset_id == asset.producer_asset_id,
        )))

    logical = _one(natural_rows(LogicalAsset), "LogicalAsset")
    _check_fields(logical, dict(
        asset_uid=asset.asset_uid, producer=asset.producer,
        source_movie_id=asset.source_movie_id, producer_asset_id=asset.producer_asset_id,
        catalog_scope=placement.catalog_scope, title_id=placement.title_id,
        title_type=placement.title_type, brand_id=placement.brand_id,
        status="active", source_movie_sha256=asset.source_movie_sha256,
    ), "LogicalAsset")

    renditions = list(session.scalars(select(AssetRendition).where(AssetRendition.asset_uid == asset.asset_uid)))
    if len(renditions) != 2 or {row.kind for row in renditions} != {"horizontal", "vertical"}:
        raise CatalogVerificationError("expected exactly horizontal and vertical renditions")
    overrides = rendition_semantic_overrides(asset.rendition_overrides)
    for row in renditions:
        rendition = getattr(asset, row.kind)
        location = getattr(expectation.verified_storage_manifest, row.kind)
        fields = dict(asset_uid=asset.asset_uid, storage_uri=location.storage_uri,
                      thumbnail_uri=location.thumbnail_uri, sha256=rendition.sha256,
                      thumbnail_sha256=rendition.thumbnail["sha256"],
                      technical_validated=rendition.technical_validated,
                      semantic_validated=rendition.semantic_validated,
                      semantic_overrides=overrides[row.kind])
        if rendition.size_bytes is not None:
            fields["size_bytes"] = rendition.size_bytes
        if rendition.thumbnail.get("size_bytes") is not None:
            fields["thumbnail_size_bytes"] = rendition.thumbnail["size_bytes"]
        _check_fields(row, fields, f"AssetRendition.{row.kind}")

    semantics = _one(list(session.scalars(select(AssetSemantics).where(
        AssetSemantics.asset_uid == asset.asset_uid))), "AssetSemantics")
    _check_fields(semantics, dict(
        asset_uid=asset.asset_uid, raw_producer_metadata=asset.raw_producer_metadata,
        visual_summary=asset.visual_summary, visible_emotions=asset.visible_emotions,
        narrative_json={} if asset.narrative_json is None else asset.narrative_json,
        relationships_json=asset.relationships_json,
        action_or_moment_complete=asset.action_or_moment_complete,
        editorial_json=asset.raw_producer_metadata.get("editorial", {}),
    ), "AssetSemantics")

    ingest = _one(natural_rows(IngestRecord), "IngestRecord")
    _check_fields(ingest, dict(asset_uid=asset.asset_uid,
        observed_package_fingerprint=expectation.observed_package_fingerprint), "IngestRecord")
    if ingest.destination_verified_at is None or ingest.catalog_committed_at is None:
        raise CatalogVerificationError("IngestRecord handoff timestamps are missing")
    if ingest.state not in {"cataloged", "cleanup_pending", "complete"}:
        raise CatalogVerificationError("IngestRecord state has not reached cataloged")
    return ingest.state
