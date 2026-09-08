"""Catalog a validated normalized producer asset without owning a transaction.

This module deliberately does not copy or inspect storage.  Its storage
locations are verified inputs from the handoff/orchestration layer.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest.fingerprint import observed_package_fingerprint
from app.storage.contracts import VerifiedRenditionLocation, VerifiedStorageManifest
from app.ingest.normalize import NormalizedAsset, NormalizedRendition
from app.models import AssetRendition, AssetSemantics, IngestRecord, LogicalAsset


class CatalogInputError(ValueError):
    """A caller supplied an invalid catalog handoff input."""


class CatalogIdentityConflict(RuntimeError):
    """A natural key already points at an unexpected Atlas identity."""


class CatalogPlacementConflict(RuntimeError):
    """An ordinary ingest attempted to move a logical asset's placement."""


class UnsupportedRenditionOverrides(CatalogInputError):
    """Non-empty producer rendition overrides have an unrecognized shape."""


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class CatalogPlacement:
    """Explicit Atlas inventory placement; it is never inferred from producer data."""

    catalog_scope: Literal["title", "brand", "general"]
    title_id: str | None = None
    title_type: Literal["movie", "series"] | None = None
    brand_id: str | None = None

    def __post_init__(self) -> None:
        if self.catalog_scope == "title":
            if not isinstance(self.title_id, str) or not self.title_id.strip():
                raise CatalogInputError("title placement requires title_id")
            if self.title_type not in {"movie", "series"}:
                raise CatalogInputError("title placement requires title_type movie or series")
            if self.brand_id is not None:
                raise CatalogInputError("title placement cannot include brand_id")
        elif self.catalog_scope == "brand":
            if not isinstance(self.brand_id, str) or not self.brand_id.strip():
                raise CatalogInputError("brand placement requires brand_id")
            if self.title_id is not None or self.title_type is not None:
                raise CatalogInputError("brand placement cannot include title fields")
        elif self.catalog_scope == "general":
            if any(value is not None for value in (self.title_id, self.title_type, self.brand_id)):
                raise CatalogInputError("general placement cannot include title or brand identifiers")
        else:
            raise CatalogInputError("catalog_scope must be title, brand, or general")


@dataclass(frozen=True)
class CatalogIngestRequest:
    normalized_asset: NormalizedAsset
    placement: CatalogPlacement
    verified_storage_manifest: VerifiedStorageManifest
    source_base_uri: str
    package_basename: str
    destination_verified_at: datetime
    observed_package_fingerprint: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_base_uri, str) or not self.source_base_uri.strip():
            raise CatalogInputError("source_base_uri must be a non-empty operational provenance value")
        if not isinstance(self.package_basename, str) or not self.package_basename.strip():
            raise CatalogInputError("package_basename must be a non-empty operational package identity")
        if not isinstance(self.destination_verified_at, datetime) or self.destination_verified_at.tzinfo is None or self.destination_verified_at.utcoffset() is None:
            raise CatalogInputError("destination_verified_at must be timezone-aware")
        if not _SHA256_HEX.fullmatch(self.observed_package_fingerprint):
            raise CatalogInputError("observed_package_fingerprint must be lowercase SHA-256 hex")
        if self.observed_package_fingerprint != observed_package_fingerprint(self.normalized_asset):
            raise CatalogInputError("observed_package_fingerprint does not match normalized_asset")


@dataclass(frozen=True)
class CatalogResult:
    asset_uid: uuid.UUID
    logical_asset_created: bool
    package_changed: bool
    ingest_state: str


def rendition_semantic_overrides(overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract the only supported override shape without guessing producer semantics."""

    if not overrides:
        return {"horizontal": {}, "vertical": {}}
    if not isinstance(overrides, dict) or not set(overrides) <= {"horizontal", "vertical"}:
        raise UnsupportedRenditionOverrides("rendition overrides may contain only horizontal and vertical keys")
    result = {"horizontal": {}, "vertical": {}}
    for kind, value in overrides.items():
        if not isinstance(value, dict):
            raise UnsupportedRenditionOverrides(f"rendition override for {kind} must be an object")
        result[kind] = value
    return result


def logical_asset_values(asset: NormalizedAsset, placement: CatalogPlacement) -> dict[str, Any]:
    """Producer-derived mutable fields and caller-defined placement for LogicalAsset."""

    return {
        "producer": asset.producer,
        "source_movie_id": asset.source_movie_id,
        "producer_asset_id": asset.producer_asset_id,
        "status": "active",
        "catalog_scope": placement.catalog_scope,
        "title_id": placement.title_id,
        "title_type": placement.title_type,
        "brand_id": placement.brand_id,
        "editorial_decision": asset.editorial_decision,
        "editorial_status": asset.editorial_status,
        "source_movie_sha256": asset.source_movie_sha256,
    }


def _present_or(value: Any, absent: Any) -> Any:
    """Substitute only an actually absent optional JSON value, preserving [] versus {}."""

    return absent if value is None else value


def semantics_values(asset: NormalizedAsset) -> dict[str, Any]:
    """Map normalized producer evidence, retaining original JSON for auditability."""

    raw_editorial = asset.raw_producer_metadata.get("editorial", {})
    return {
        "visual_summary": asset.visual_summary,
        "subjects": asset.subjects,
        "objects": asset.objects,
        "actions": asset.actions,
        "visible_emotions": asset.visible_emotions,
        "interactions": asset.interactions,
        "setting": asset.setting,
        "people_json": asset.people_json,
        "relationships_json": asset.relationships_json,
        "keywords": [],
        "search_terms": asset.search_terms,
        "negative_use_cases": asset.negative_use_cases,
        "standalone_meaning": asset.standalone_meaning,
        "reusable_broll": asset.reusable_broll,
        "action_or_moment_complete": asset.action_or_moment_complete,
        "narrative_json": _present_or(asset.narrative_json, {}),
        "provenance_json": {
            "source_timeline": _present_or(asset.source_timeline, {}),
            "producer": asset.producer,
            "producer_version": asset.producer_provenance.get("producer_version"),
            "audio": _present_or(asset.producer_provenance.get("audio"), {}),
            "export": _present_or(asset.producer_provenance.get("export"), {}),
        },
        "editorial_json": raw_editorial,
        "search_text": None,
        "embedding_text": None,
        "raw_producer_metadata": asset.raw_producer_metadata,
    }


def rendition_values(rendition: NormalizedRendition, location: VerifiedRenditionLocation, overrides: dict[str, Any]) -> dict[str, Any]:
    """Map producer technical metadata and supplied verified locations for a rendition."""

    thumbnail = rendition.thumbnail
    return {
        "storage_uri": location.storage_uri,
        "thumbnail_uri": location.thumbnail_uri,
        "sha256": rendition.sha256,
        "thumbnail_sha256": thumbnail.get("sha256"),
        "size_bytes": rendition.size_bytes,
        "thumbnail_size_bytes": thumbnail.get("size_bytes"),
        "mime_type": rendition.mime_type,
        "duration_seconds": rendition.duration_seconds,
        "width": rendition.width,
        "height": rendition.height,
        "fps": rendition.fps,
        "orientation": rendition.orientation,
        "aspect_ratio": rendition.aspect_ratio,
        "technical_validated": rendition.technical_validated,
        "semantic_validated": rendition.semantic_validated,
        "semantic_overrides": overrides,
    }


def ingest_transition(existing_state: str | None, package_changed: bool) -> tuple[str, bool]:
    """Return the new state and whether catalog handoff timestamps should be refreshed."""

    if existing_state is None or package_changed:
        return "cataloged", True
    if existing_state in {"cleanup_pending", "complete"}:
        return existing_state, False
    if existing_state == "cataloged":
        return "cataloged", False
    return "cataloged", True


def _same_placement(existing: LogicalAsset, placement: CatalogPlacement) -> bool:
    return (existing.catalog_scope, existing.title_id, existing.title_type, existing.brand_id) == (
        placement.catalog_scope, placement.title_id, placement.title_type, placement.brand_id
    )


def _apply_values(target: Any, values: dict[str, Any]) -> None:
    for name, value in values.items():
        setattr(target, name, value)


def catalog_validated_asset(session: Session, request: CatalogIngestRequest) -> CatalogResult:
    """Upsert one validated asset and its current handoff record; never commit or roll back.

    Concurrent absent-row insert races are intentionally left to the database's
    unique constraints and a future orchestrator retry policy.
    """

    asset = request.normalized_asset
    overrides = rendition_semantic_overrides(asset.rendition_overrides)
    natural_key = (asset.producer, asset.source_movie_id, asset.producer_asset_id)
    logical = session.scalar(select(LogicalAsset).where(
        LogicalAsset.producer == natural_key[0],
        LogicalAsset.source_movie_id == natural_key[1],
        LogicalAsset.producer_asset_id == natural_key[2],
    ))
    ingest = session.scalar(select(IngestRecord).where(
        IngestRecord.producer == natural_key[0],
        IngestRecord.source_movie_id == natural_key[1],
        IngestRecord.producer_asset_id == natural_key[2],
    ))
    if ingest is not None and ingest.asset_uid is not None and ingest.asset_uid != asset.asset_uid:
        raise CatalogIdentityConflict("existing ingest natural key has a different asset_uid")
    logical_created = logical is None
    if logical is None:
        logical = LogicalAsset(asset_uid=asset.asset_uid, **logical_asset_values(asset, request.placement))
        session.add(logical)
    else:
        if logical.asset_uid != asset.asset_uid:
            raise CatalogIdentityConflict("existing natural key has a different asset_uid")
        if not _same_placement(logical, request.placement):
            raise CatalogPlacementConflict("existing natural key has a different catalog placement")
        _apply_values(logical, logical_asset_values(asset, request.placement))

    existing_renditions = {
        rendition.kind: rendition
        for rendition in session.scalars(select(AssetRendition).where(AssetRendition.asset_uid == asset.asset_uid))
    }
    for kind, normalized_rendition, location in (
        ("horizontal", asset.horizontal, request.verified_storage_manifest.horizontal),
        ("vertical", asset.vertical, request.verified_storage_manifest.vertical),
    ):
        rendition = existing_renditions.get(kind)
        values = rendition_values(normalized_rendition, location, overrides[kind])
        if rendition is None:
            session.add(AssetRendition(asset_uid=asset.asset_uid, kind=kind, **values))
        else:
            _apply_values(rendition, values)

    semantics = session.get(AssetSemantics, asset.asset_uid)
    if semantics is None:
        session.add(AssetSemantics(asset_uid=asset.asset_uid, **semantics_values(asset)))
    else:
        _apply_values(semantics, semantics_values(asset))

    package_changed = ingest is None or ingest.observed_package_fingerprint != request.observed_package_fingerprint
    current_state, refresh_handoff = ingest_transition(ingest.state if ingest else None, package_changed)
    if ingest is None:
        ingest = IngestRecord(
            asset_uid=asset.asset_uid, producer=asset.producer, source_movie_id=asset.source_movie_id,
            producer_asset_id=asset.producer_asset_id, source_base_uri=request.source_base_uri,
            package_basename=request.package_basename, state=current_state,
            destination_verified_at=request.destination_verified_at,
            catalog_committed_at=datetime.now(timezone.utc), source_released_at=None,
            last_error=None, observed_package_fingerprint=request.observed_package_fingerprint,
        )
        session.add(ingest)
    elif package_changed or refresh_handoff:
        ingest.asset_uid = asset.asset_uid
        ingest.source_base_uri = request.source_base_uri
        ingest.package_basename = request.package_basename
        ingest.state = current_state
        ingest.destination_verified_at = request.destination_verified_at
        ingest.catalog_committed_at = datetime.now(timezone.utc)
        ingest.source_released_at = None
        ingest.last_error = None
        ingest.observed_package_fingerprint = request.observed_package_fingerprint
    elif ingest.asset_uid is None:
        # Repair an older incomplete handoff without treating an identical,
        # already-complete package as a new catalog commit.
        ingest.asset_uid = asset.asset_uid

    session.flush()
    return CatalogResult(asset_uid=asset.asset_uid, logical_asset_created=logical_created, package_changed=package_changed, ingest_state=current_state)
