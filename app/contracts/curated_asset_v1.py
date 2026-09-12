"""Small explicit, non-AI contract for curated Atlas assets."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CuratedRenditionV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["horizontal", "vertical"]
    filename: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    mime_type: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    orientation: str | None = None
    aspect_ratio: str | None = None
    thumbnail: dict[str, Any] = Field(default_factory=dict)
    technical_validated: bool = True
    semantic_validated: bool = False


class CuratedAssetV1(BaseModel):
    """Explicit editorial/import-list evidence; no pixel or AI inference is implied."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["curated_asset_v1"]
    producer: Literal["kurukin_curated"] = "kurukin_curated"
    collection_id: str
    producer_asset_id: str
    renditions: list[CuratedRenditionV1]
    visual_summary: str | None = None
    keywords: list[Any] = Field(default_factory=list)
    search_terms: list[Any] = Field(default_factory=list)
    negative_use_cases: list[Any] = Field(default_factory=list)
    standalone_meaning: str | None = None
    metadata_source: Literal["editorial", "filename", "collection_defaults", "none"] = "none"
    collection_metadata: dict[str, Any] = Field(default_factory=dict)
    editorial_decision: str | None = None
    editorial_status: str | None = None

    @model_validator(mode="after")
    def validate_identity_and_renditions(self) -> "CuratedAssetV1":
        for name in ("collection_id", "producer_asset_id"):
            value = getattr(self, name)
            if not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be a non-empty normalized string")
        if not self.renditions:
            raise ValueError("curated asset requires at least one rendition")
        kinds = [rendition.kind for rendition in self.renditions]
        if len(kinds) != len(set(kinds)):
            raise ValueError("curated asset cannot contain duplicate rendition kinds")
        has_semantic_claims = any((
            self.visual_summary, self.keywords, self.search_terms,
            self.negative_use_cases, self.standalone_meaning,
        ))
        if has_semantic_claims and self.metadata_source == "none":
            raise ValueError("curated semantic claims require a metadata_source")
        return self
