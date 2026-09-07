"""Pure application tests for the P0 catalog writer inputs and mappings."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.contracts.asset_metadata_v1 import AssetMetadataV1
from app.ingest.catalog import (
    CatalogIngestRequest,
    CatalogInputError,
    CatalogPlacement,
    UnsupportedRenditionOverrides,
    VerifiedRenditionLocation,
    VerifiedStorageManifest,
    ingest_transition,
    logical_asset_values,
    observed_package_fingerprint,
    rendition_semantic_overrides,
    semantics_values,
)
from app.ingest.normalize import normalize_metadata


GOLDEN = Path(__file__).parent / "fixtures/mbe/rc162/rc162-pareja-conversando-de-manera-cercana-en-un-entor.json"


def normalized(raw: dict | None = None):
    raw = json.loads(GOLDEN.read_text()) if raw is None else raw
    return normalize_metadata(AssetMetadataV1.model_validate(raw), raw)


def locations() -> VerifiedStorageManifest:
    return VerifiedStorageManifest(
        horizontal=VerifiedRenditionLocation("atlas://final/h.mp4", "atlas://final/h.jpg"),
        vertical=VerifiedRenditionLocation("atlas://final/v.mp4", "atlas://final/v.jpg"),
    )


def test_catalog_placement_invariants() -> None:
    assert CatalogPlacement("title", title_id="title", title_type="movie").catalog_scope == "title"
    assert CatalogPlacement("brand", brand_id="brand").brand_id == "brand"
    assert CatalogPlacement("general").catalog_scope == "general"
    with pytest.raises(CatalogInputError):
        CatalogPlacement("title", title_type="movie")
    with pytest.raises(CatalogInputError):
        CatalogPlacement("title", title_id="title")
    with pytest.raises(CatalogInputError):
        CatalogPlacement("title", title_id="   ", title_type="movie")
    with pytest.raises(CatalogInputError):
        CatalogPlacement("brand", brand_id="brand", title_id="title")
    with pytest.raises(CatalogInputError):
        CatalogPlacement("brand", brand_id="   ")
    with pytest.raises(CatalogInputError):
        CatalogPlacement("general", brand_id="brand")


def test_verified_storage_requires_exact_nonempty_locations_and_never_uses_source_uri() -> None:
    with pytest.raises(CatalogInputError):
        VerifiedRenditionLocation("", "atlas://final/h.jpg")
    with pytest.raises(CatalogInputError):
        VerifiedStorageManifest(horizontal=VerifiedRenditionLocation("a", "b"), vertical=None)  # type: ignore[arg-type]
    request = CatalogIngestRequest(
        normalized_asset=normalized(), placement=CatalogPlacement("general"), verified_storage_manifest=locations(),
        source_base_uri="file:///mbe/outbox/rc162", package_basename="rc162", destination_verified_at=datetime.now(timezone.utc),
        observed_package_fingerprint=observed_package_fingerprint(normalized()),
    )
    assert request.verified_storage_manifest.horizontal.storage_uri != request.source_base_uri
    with pytest.raises(CatalogInputError):
        CatalogIngestRequest(
            normalized_asset=normalized(), placement=CatalogPlacement("general"), verified_storage_manifest=locations(),
            source_base_uri="source", package_basename="package", destination_verified_at=datetime.now(),
            observed_package_fingerprint=observed_package_fingerprint(normalized()),
        )


def test_request_fingerprint_must_match_the_normalized_asset() -> None:
    asset_a = normalized()
    fingerprint_a = observed_package_fingerprint(asset_a)
    accepted = CatalogIngestRequest(
        normalized_asset=asset_a, placement=CatalogPlacement("general"), verified_storage_manifest=locations(),
        source_base_uri="source", package_basename="package", destination_verified_at=datetime.now(timezone.utc),
        observed_package_fingerprint=fingerprint_a,
    )
    assert accepted.observed_package_fingerprint == fingerprint_a

    with pytest.raises(CatalogInputError):
        CatalogIngestRequest(
            normalized_asset=asset_a, placement=CatalogPlacement("general"), verified_storage_manifest=locations(),
            source_base_uri="source", package_basename="package", destination_verified_at=datetime.now(timezone.utc),
            observed_package_fingerprint="a" * 64,
        )

    raw_b = copy.deepcopy(asset_a.raw_producer_metadata)
    raw_b["visual"]["summary_es"] = "modified"
    asset_b = normalized(raw_b)
    with pytest.raises(CatalogInputError):
        CatalogIngestRequest(
            normalized_asset=asset_b, placement=CatalogPlacement("general"), verified_storage_manifest=locations(),
            source_base_uri="source", package_basename="package", destination_verified_at=datetime.now(timezone.utc),
            observed_package_fingerprint=fingerprint_a,
        )


def test_package_fingerprint_is_stable_sensitive_and_sha256_hex() -> None:
    asset = normalized()
    fingerprint = observed_package_fingerprint(asset)
    assert fingerprint == observed_package_fingerprint(asset)
    assert len(fingerprint) == 64 and fingerprint == fingerprint.lower()
    assert all(char in "0123456789abcdef" for char in fingerprint)

    changed_semantics = copy.deepcopy(asset.raw_producer_metadata)
    changed_semantics["visual"]["summary_es"] = "changed"
    assert observed_package_fingerprint(normalized(changed_semantics)) != fingerprint

    changed_rendition = copy.deepcopy(asset.raw_producer_metadata)
    changed_rendition["media"]["horizontal"]["sha256"] = "a" * 64
    assert observed_package_fingerprint(normalized(changed_rendition)) != fingerprint


def test_rendition_override_extraction_is_conservative() -> None:
    assert rendition_semantic_overrides({}) == {"horizontal": {}, "vertical": {}}
    assert rendition_semantic_overrides({"vertical": {"caption": "x"}}) == {
        "horizontal": {}, "vertical": {"caption": "x"}
    }
    with pytest.raises(UnsupportedRenditionOverrides):
        rendition_semantic_overrides({"square": {}})
    with pytest.raises(UnsupportedRenditionOverrides):
        rendition_semantic_overrides({"vertical": ["not-an-object"]})


def test_mapping_preserves_identity_visual_narrative_and_raw_editorial() -> None:
    asset = normalized()
    logical = logical_asset_values(asset, CatalogPlacement("title", title_id="romper-el-circulo", title_type="movie"))
    assert (logical["producer"], logical["source_movie_id"], logical["producer_asset_id"]) == (
        asset.producer, asset.source_movie_id, asset.producer_asset_id
    )
    assert asset.asset_uid == normalized().asset_uid
    semantics = semantics_values(asset)
    assert semantics["narrative_json"]["tone"] == ["angry"]
    assert "angry" not in semantics["visible_emotions"]
    assert semantics["editorial_json"]["action_or_moment_complete"] == "true"


@pytest.mark.parametrize(
    ("state", "changed", "expected_state", "refresh"),
    [
        (None, False, "cataloged", True),
        ("cataloged", False, "cataloged", False),
        ("cleanup_pending", False, "cleanup_pending", False),
        ("complete", False, "complete", False),
        ("complete", True, "cataloged", True),
    ],
)
def test_ingest_state_transition(state, changed, expected_state, refresh) -> None:
    assert ingest_transition(state, changed) == (expected_state, refresh)
