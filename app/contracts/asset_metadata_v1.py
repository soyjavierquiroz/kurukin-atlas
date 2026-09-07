"""Focused, tolerant Atlas consumer types for frozen ``asset_metadata_v1``."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProducerModel(BaseModel):
    """Accept producer additions; Atlas retains the original raw document separately."""

    model_config = ConfigDict(extra="allow")


class Analysis(ProducerModel):
    producer: str
    producer_version: str | None = None
    semantic_ready: bool
    final_asset_semantics_validated: bool


class Asset(ProducerModel):
    id: str
    slug: str | None = None
    source_movie_id: str


class Source(ProducerModel):
    movie_sha256: str


class Thumbnail(ProducerModel):
    file: str
    sha256: str
    size_bytes: int | None = None
    mime_type: str | None = None


class Rendition(ProducerModel):
    file: str
    sha256: str
    size_bytes: int | None = None
    mime_type: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    orientation: str | None = None
    aspect_ratio: str | None = None
    technical_validated: bool
    semantic_validated: bool
    thumbnail: Thumbnail


class Media(ProducerModel):
    horizontal: Rendition
    vertical: Rendition


class Editorial(ProducerModel):
    decision: str
    status: str | None = None
    reusable_broll: bool | None = None
    action_or_moment_complete: Any = None
    search_terms_es: list[Any] = Field(default_factory=list)
    negative_use_cases_es: list[Any] = Field(default_factory=list)
    standalone_meaning_es: str | None = None

    @property
    def normalized_action_or_moment_complete(self) -> bool | None:
        value = self.action_or_moment_complete
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized == "true":
                return True
            if normalized == "false":
                return False
        return None


class Visual(ProducerModel):
    summary_es: str | None = None
    subjects: list[Any] = Field(default_factory=list)
    objects: list[Any] = Field(default_factory=list)
    actions: list[Any] = Field(default_factory=list)
    visible_emotions: list[Any] = Field(default_factory=list)
    interaction_labels: list[Any] = Field(default_factory=list)
    setting: str | None = None
    people: dict[str, Any] = Field(default_factory=dict)
    relationships: list[Any] = Field(default_factory=list)
    rendition_overrides: dict[str, Any] = Field(default_factory=dict)
    final_vertical: dict[str, Any] | None = None


class AssetMetadataV1(ProducerModel):
    schema_version: str
    analysis: Analysis
    asset: Asset
    source: Source
    editorial: Editorial
    media: Media
    visual: Visual
    narrative: dict[str, Any] | list[Any] | None = None
    source_timeline: dict[str, Any] | list[Any] | None = None
    audio: dict[str, Any] | list[Any] | None = None
    export: dict[str, Any] | list[Any] | None = None

