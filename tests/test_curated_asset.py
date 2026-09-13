"""Curated normalization and rendition-generic catalog seams; no live database."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

from app.contracts.curated_asset_v1 import CuratedAssetV1
from app.ingest.catalog import (
    CatalogIngestRequest,
    CatalogInputError,
    CatalogPlacement,
    UnsupportedRenditionOverrides,
    catalog_validated_asset,
    logical_asset_values,
    rendition_semantic_overrides,
    rendition_values,
    semantics_values,
)
from app.ingest.curated import normalize_curated_asset
from app.ingest.fingerprint import observed_package_fingerprint
from app.ingest.normalize import atlas_asset_uid
from app.ingest.verification import verify_cataloged_asset
from app.models import AssetRendition, IngestRecord, LogicalAsset
from app.storage.contracts import VerifiedRenditionLocation, VerifiedStorageManifest


SHA = "a" * 64


def curated(collection: str = "deluxe", filename: str = "luxury_000014_mix_576x1024.mp4", source: str = "editorial"):
    return CuratedAssetV1.model_validate({
        "schema_version": "curated_asset_v1", "collection_id": collection,
        "producer_asset_id": filename.removesuffix(".mp4"),
        "renditions": [{"kind": "vertical", "filename": filename, "sha256": SHA, "size_bytes": 42,
                        "width": 576, "height": 1024, "orientation": "vertical"}],
        "keywords": ["luxury"] if collection == "deluxe" else ["flowers"],
        "search_terms": ["premium"] if collection == "deluxe" else ["blue flowers"],
        "metadata_source": source,
        "collection_metadata": {"declared_by": source},
    })


def vertical_manifest() -> VerifiedStorageManifest:
    return VerifiedStorageManifest({"vertical": VerifiedRenditionLocation("atlas://final/v.mp4", None)})


def request(asset):
    return CatalogIngestRequest(
        normalized_asset=asset, placement=CatalogPlacement("general"), verified_storage_manifest=vertical_manifest(),
        source_base_uri="import://curated", package_basename=asset.producer_asset_id,
        destination_verified_at=datetime.now(timezone.utc), observed_package_fingerprint=observed_package_fingerprint(asset),
    )


def test_curated_deluxe_normalizes_vertical_only_without_fabrication() -> None:
    asset = normalize_curated_asset(curated())
    assert (asset.producer, asset.source_kind, asset.source_key) == (
        "kurukin_curated", "collection", "collection:deluxe"
    )
    assert set(asset.renditions) == {"vertical"}
    assert asset.horizontal is None
    assert asset.renditions["vertical"].thumbnail == {}
    assert asset.asset_uid == atlas_asset_uid("kurukin_curated", "collection:deluxe", "luxury_000014_mix_576x1024")
    assert asset.producer_provenance["metadata_source"] == "editorial"
    assert asset.people_json == {} and asset.visible_emotions == [] and asset.relationships_json == []


def test_curated_nature_preserves_filename_provenance_and_search_terms() -> None:
    asset = normalize_curated_asset(curated("nature", "nature_000758_many_blue_flowers_growing_side_path.mp4", "filename"))
    assert asset.source_key == "collection:nature"
    assert asset.search_terms == ["blue flowers"]
    assert asset.producer_provenance["metadata_source"] == "filename"
    assert semantics_values(asset)["provenance_json"]["producer_metadata"]["metadata_source"] == "filename"


def test_vertical_manifest_and_catalog_request_require_exact_rendition_set() -> None:
    asset = normalize_curated_asset(curated())
    assert vertical_manifest().renditions.keys() == {"vertical"}
    missing = VerifiedStorageManifest({"horizontal": VerifiedRenditionLocation("a", "b")})
    extra = VerifiedStorageManifest({
        "horizontal": VerifiedRenditionLocation("a", "b"),
        "vertical": VerifiedRenditionLocation("c", "d"),
    })
    for manifest in (missing, extra):
        with pytest.raises(CatalogInputError, match="exactly match"):
            CatalogIngestRequest(
                normalized_asset=asset, placement=CatalogPlacement("general"), verified_storage_manifest=manifest,
                source_base_uri="source", package_basename="package", destination_verified_at=datetime.now(timezone.utc),
                observed_package_fingerprint=observed_package_fingerprint(asset),
            )


def test_thumbnail_evidence_and_verified_location_must_agree() -> None:
    asset = normalize_curated_asset(curated())
    with pytest.raises(CatalogInputError, match="no thumbnail evidence"):
        CatalogIngestRequest(
            normalized_asset=asset, placement=CatalogPlacement("general"),
            verified_storage_manifest=VerifiedStorageManifest({"vertical": VerifiedRenditionLocation("v", "v.jpg")}),
            source_base_uri="source", package_basename="package", destination_verified_at=datetime.now(timezone.utc),
            observed_package_fingerprint=observed_package_fingerprint(asset),
        )
    data = curated().model_dump(mode="python")
    data["renditions"][0]["thumbnail"] = {"file": "v.jpg", "sha256": SHA, "size_bytes": 3}
    with_thumbnail = normalize_curated_asset(CuratedAssetV1.model_validate(data))
    with pytest.raises(CatalogInputError, match="requires a verified thumbnail_uri"):
        request(with_thumbnail)


def test_override_for_absent_rendition_is_rejected() -> None:
    asset = normalize_curated_asset(curated())
    with pytest.raises(UnsupportedRenditionOverrides, match="absent rendition"):
        rendition_semantic_overrides({"horizontal": {}}, set(asset.renditions))
    assert rendition_semantic_overrides({}, set(asset.renditions)) == {"vertical": {}}
    object.__setattr__(asset, "rendition_overrides", {"horizontal": {}})
    with pytest.raises(UnsupportedRenditionOverrides, match="absent rendition"):
        request(asset)


def test_curated_contract_rejects_duplicate_or_unsupported_rendition_kinds() -> None:
    data = curated().model_dump(mode="python")
    data["renditions"].append(dict(data["renditions"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        CuratedAssetV1.model_validate(data)
    data = curated().model_dump(mode="python")
    data["renditions"][0]["kind"] = "square"
    with pytest.raises(ValueError):
        CuratedAssetV1.model_validate(data)
    data = curated().model_dump(mode="python")
    data["metadata_source"] = "none"
    with pytest.raises(ValueError, match="semantic claims"):
        CuratedAssetV1.model_validate(data)


def test_writer_removes_stale_horizontal_and_preserves_vertical_uid() -> None:
    asset = normalize_curated_asset(curated())
    catalog_request = request(asset)
    logical = LogicalAsset(asset_uid=asset.asset_uid, **logical_asset_values(asset, catalog_request.placement))
    ingest = IngestRecord(
        asset_uid=asset.asset_uid, producer=asset.producer, source_key=asset.source_key, source_kind=asset.source_kind,
        producer_asset_id=asset.producer_asset_id, source_base_uri="old", package_basename="old", state="cataloged",
        observed_package_fingerprint=catalog_request.observed_package_fingerprint,
    )
    old_horizontal = AssetRendition(
        rendition_uid=uuid.uuid4(), asset_uid=asset.asset_uid, kind="horizontal", storage_uri="old-h", thumbnail_uri="old-h-t",
        sha256=SHA, thumbnail_sha256=None, technical_validated=True, semantic_validated=False, semantic_overrides={},
    )
    vertical_uid = uuid.uuid4()
    old_vertical = AssetRendition(
        rendition_uid=vertical_uid, asset_uid=asset.asset_uid, kind="vertical", storage_uri="old-v", thumbnail_uri="old-v-t",
        sha256=SHA, thumbnail_sha256=None, technical_validated=True, semantic_validated=False, semantic_overrides={},
    )
    session = Mock()
    session.scalar.side_effect = [logical, ingest]
    session.scalars.return_value = [old_horizontal, old_vertical]
    session.get.return_value = None

    catalog_validated_asset(session, catalog_request)

    assert old_vertical.rendition_uid == vertical_uid
    assert old_vertical.storage_uri == "atlas://final/v.mp4"
    assert old_vertical.thumbnail_uri is None
    assert old_vertical.thumbnail_sha256 is None and old_vertical.thumbnail_size_bytes is None
    session.delete.assert_called_once_with(old_horizontal)
    session.flush.assert_called_once()


def test_verifier_accepts_exactly_one_vertical_rendition() -> None:
    asset = normalize_curated_asset(curated())
    catalog_request = request(asset)
    rendition = asset.renditions["vertical"]
    row = SimpleNamespace(asset_uid=asset.asset_uid, kind="vertical", **rendition_values(
        rendition, catalog_request.verified_storage_manifest.renditions["vertical"], {}
    ))
    logical = SimpleNamespace(asset_uid=asset.asset_uid, **logical_asset_values(asset, catalog_request.placement))
    semantics = SimpleNamespace(asset_uid=asset.asset_uid, **semantics_values(asset))
    ingest = SimpleNamespace(asset_uid=asset.asset_uid, source_kind=asset.source_kind,
                             observed_package_fingerprint=catalog_request.observed_package_fingerprint,
                             destination_verified_at=catalog_request.destination_verified_at,
                             catalog_committed_at=catalog_request.destination_verified_at, state="cataloged")
    session = Mock(scalars=Mock(side_effect=[[logical], [row], [semantics], [ingest]]))
    assert verify_cataloged_asset(session, catalog_request) == "cataloged"


def test_identical_replay_syncs_ingest_source_kind_without_touching_handoff() -> None:
    asset = normalize_curated_asset(curated())
    catalog_request = request(asset)
    logical = LogicalAsset(asset_uid=asset.asset_uid, **logical_asset_values(asset, catalog_request.placement))
    verified_at = datetime(2026, 9, 12, tzinfo=timezone.utc)
    committed_at = datetime(2026, 9, 13, tzinfo=timezone.utc)
    ingest = IngestRecord(
        asset_uid=asset.asset_uid, producer=asset.producer, source_key=asset.source_key, source_kind="movie",
        producer_asset_id=asset.producer_asset_id, source_base_uri="old", package_basename="old", state="cleanup_pending",
        destination_verified_at=verified_at, catalog_committed_at=committed_at,
        observed_package_fingerprint=catalog_request.observed_package_fingerprint,
    )
    old_vertical = AssetRendition(
        rendition_uid=uuid.uuid4(), asset_uid=asset.asset_uid, kind="vertical", storage_uri="old-v", thumbnail_uri=None,
        sha256=SHA, thumbnail_sha256=None, technical_validated=True, semantic_validated=False, semantic_overrides={},
    )
    session = Mock()
    session.scalar.side_effect = [logical, ingest]
    session.scalars.return_value = [old_vertical]
    session.get.return_value = None

    result = catalog_validated_asset(session, catalog_request)

    assert result.ingest_state == "cleanup_pending"
    assert ingest.source_kind == "collection"
    assert (ingest.destination_verified_at, ingest.catalog_committed_at) == (verified_at, committed_at)
