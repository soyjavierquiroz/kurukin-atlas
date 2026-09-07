"""Producer-neutral in-memory result for a later database ingest milestone."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.contracts.asset_metadata_v1 import AssetMetadataV1, Rendition

ATLAS_ASSET_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:kurukin:atlas:asset:v1")


def atlas_asset_uid(producer: str, source_movie_id: str, producer_asset_id: str) -> uuid.UUID:
    """UUIDv5 for the natural key, never a filename, path, slug, or Drive identifier."""

    return uuid.uuid5(ATLAS_ASSET_NAMESPACE, f"{producer}\x1f{source_movie_id}\x1f{producer_asset_id}")


@dataclass(frozen=True)
class NormalizedRendition:
    kind: str
    filename: str
    sha256: str
    size_bytes: int | None
    mime_type: str | None
    duration_seconds: float | None
    width: int | None
    height: int | None
    fps: float | None
    orientation: str | None
    aspect_ratio: str | None
    technical_validated: bool
    semantic_validated: bool
    thumbnail: dict[str, Any]


@dataclass(frozen=True)
class NormalizedAsset:
    asset_uid: uuid.UUID
    producer: str
    producer_asset_id: str
    source_movie_id: str
    source_movie_sha256: str
    slug: str | None
    editorial_decision: str
    editorial_status: str | None
    reusable_broll: bool | None
    standalone_meaning: str | None
    action_or_moment_complete: bool | None
    search_terms: list[Any]
    negative_use_cases: list[Any]
    visual_summary: str | None
    subjects: list[Any]
    objects: list[Any]
    actions: list[Any]
    visible_emotions: list[Any]
    interactions: list[Any]
    setting: str | None
    people_json: dict[str, Any]
    relationships_json: list[Any]
    rendition_overrides: dict[str, Any]
    narrative_json: dict[str, Any] | list[Any] | None
    source_timeline: dict[str, Any] | list[Any] | None
    producer_provenance: dict[str, Any]
    horizontal: NormalizedRendition
    vertical: NormalizedRendition
    raw_producer_metadata: dict[str, Any]


def _rendition(kind: str, item: Rendition) -> NormalizedRendition:
    return NormalizedRendition(kind, item.file, item.sha256, item.size_bytes, item.mime_type,
                               item.duration_seconds, item.width, item.height, item.fps,
                               item.orientation, item.aspect_ratio, item.technical_validated,
                               item.semantic_validated, item.thumbnail.model_dump(mode="python"))


def normalize_metadata(metadata: AssetMetadataV1, raw_metadata: dict[str, Any]) -> NormalizedAsset:
    """Keep visual and narrative evidence separate and retain complete raw producer JSON."""

    visual = metadata.visual
    return NormalizedAsset(
        asset_uid=atlas_asset_uid(metadata.analysis.producer, metadata.asset.source_movie_id, metadata.asset.id),
        producer=metadata.analysis.producer, producer_asset_id=metadata.asset.id,
        source_movie_id=metadata.asset.source_movie_id, source_movie_sha256=metadata.source.movie_sha256,
        slug=metadata.asset.slug, editorial_decision=metadata.editorial.decision,
        editorial_status=metadata.editorial.status, reusable_broll=metadata.editorial.reusable_broll,
        standalone_meaning=metadata.editorial.standalone_meaning_es,
        action_or_moment_complete=metadata.editorial.normalized_action_or_moment_complete,
        search_terms=metadata.editorial.search_terms_es, negative_use_cases=metadata.editorial.negative_use_cases_es,
        visual_summary=visual.summary_es, subjects=visual.subjects, objects=visual.objects, actions=visual.actions,
        visible_emotions=visual.visible_emotions, interactions=visual.interaction_labels, setting=visual.setting,
        people_json=visual.people, relationships_json=visual.relationships,
        rendition_overrides=visual.rendition_overrides, narrative_json=metadata.narrative,
        source_timeline=metadata.source_timeline,
        producer_provenance={"producer_version": metadata.analysis.producer_version, "audio": metadata.audio, "export": metadata.export},
        horizontal=_rendition("horizontal", metadata.media.horizontal), vertical=_rendition("vertical", metadata.media.vertical),
        raw_producer_metadata=raw_metadata,
    )
