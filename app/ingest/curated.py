"""Normalization for explicitly described curated collections; never invokes AI."""

from __future__ import annotations

from typing import Any

from app.contracts.curated_asset_v1 import CuratedAssetV1
from app.ingest.normalize import NormalizedAsset, NormalizedRendition, atlas_asset_uid


def normalize_curated_asset(metadata: CuratedAssetV1, raw_metadata: dict[str, Any] | None = None) -> NormalizedAsset:
    """Map editorial or filename-derived input into the common Atlas representation."""

    source_key = f"collection:{metadata.collection_id}"
    renditions = {
        item.kind: NormalizedRendition(
            kind=item.kind, filename=item.filename, sha256=item.sha256, size_bytes=item.size_bytes,
            mime_type=item.mime_type, duration_seconds=item.duration_seconds, width=item.width,
            height=item.height, fps=item.fps, orientation=item.orientation,
            aspect_ratio=item.aspect_ratio, technical_validated=item.technical_validated,
            semantic_validated=item.semantic_validated, thumbnail=item.thumbnail,
        )
        for item in metadata.renditions
    }
    raw = metadata.model_dump(mode="python") if raw_metadata is None else raw_metadata
    provenance = {
        "metadata_source": metadata.metadata_source,
        "collection_id": metadata.collection_id,
        "collection_metadata": metadata.collection_metadata,
        "semantic_inference": "none",
    }
    return NormalizedAsset(
        asset_uid=atlas_asset_uid(metadata.producer, source_key, metadata.producer_asset_id),
        producer=metadata.producer, producer_asset_id=metadata.producer_asset_id,
        source_key=source_key, source_kind="collection", source_movie_sha256=None,
        slug=None, editorial_decision=metadata.editorial_decision, editorial_status=metadata.editorial_status,
        reusable_broll=None, standalone_meaning=metadata.standalone_meaning,
        action_or_moment_complete=None, keywords=metadata.keywords, search_terms=metadata.search_terms,
        negative_use_cases=metadata.negative_use_cases, visual_summary=metadata.visual_summary,
        subjects=[], objects=[], actions=[], visible_emotions=[], interactions=[], setting=None,
        people_json={}, relationships_json=[], rendition_overrides={}, narrative_json=None,
        source_timeline=None, producer_provenance=provenance, renditions=renditions,
        raw_producer_metadata=raw,
    )
