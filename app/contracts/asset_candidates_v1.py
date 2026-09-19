"""Versioned response contract for Atlas local asset candidates."""

from __future__ import annotations

from math import isfinite
from numbers import Real
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


MatchState = Literal["MATCH", "UNKNOWN", "MISMATCH"]
Confidence = Literal["high", "uncertain"]
ScarcityReason = Literal[
    "no_candidates_in_scope", "no_requirement_match", "no_high_confidence_match", "no_acceptable_rendition",
]


def normalize_duration_seconds(value: Any) -> float | None:
    """Return only a finite, positive stored rendition duration for public output."""

    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    duration = float(value)
    return duration if isfinite(duration) and duration > 0 else None


class CatalogPlacementV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    catalog_scope: Literal["title", "brand", "general"]
    title_id: str | None = None
    brand_id: str | None = None


class SourceProvenanceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_kind: str
    source_key: str
    producer_asset_id: str


class SelectedRenditionV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["horizontal", "vertical"]
    content_locator: str
    thumbnail_locator: str | None = None
    duration_seconds: float | None = None

    @field_validator("duration_seconds", mode="before")
    @classmethod
    def normalize_duration(cls, value: Any) -> float | None:
        return normalize_duration_seconds(value)


class ProducerEditorialEvidenceV1(BaseModel):
    """Stored normalized evidence only; no claim is derived from the request query."""

    model_config = ConfigDict(extra="forbid")
    visual_summary: str | None = None
    keywords: list[Any] = Field(default_factory=list)
    search_terms: list[Any] = Field(default_factory=list)
    subjects: list[Any] = Field(default_factory=list)
    objects: list[Any] = Field(default_factory=list)
    actions: list[Any] = Field(default_factory=list)
    setting: str | None = None
    visible_emotions: list[Any] = Field(default_factory=list)
    relationships: list[Any] = Field(default_factory=list)
    people: dict[str, Any] = Field(default_factory=dict)
    negative_use_cases: list[Any] = Field(default_factory=list)
    standalone_meaning: str | None = None
    editorial: dict[str, Any] = Field(default_factory=dict)
    rendition_override: dict[str, Any] = Field(default_factory=dict)


class RequirementMatchV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: MatchState
    evidence: list[str] = Field(default_factory=list)
    detail: str | None = None


class ScoreComponentsV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lexical_relevance: float
    structured_match: float
    rendition_preference: float
    editorial_signal: float


class AssetCandidateV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_uid: str
    producer: str
    source_provenance: SourceProvenanceV1
    catalog_placement: CatalogPlacementV1
    selected_rendition: SelectedRenditionV1
    evidence: ProducerEditorialEvidenceV1
    requirement_matches: dict[str, RequirementMatchV1]
    contradictions: list[str] = Field(default_factory=list)
    confidence: Confidence
    score_components: ScoreComponentsV1
    atlas_local_score: float


class AssetCandidatesV1(BaseModel):
    model_config = ConfigDict(extra="forbid", title="asset_candidates_v1")
    schema_version: Literal["asset_candidates_v1"] = "asset_candidates_v1"
    candidates: list[AssetCandidateV1] = Field(default_factory=list)
    scarcity_reason: ScarcityReason | None = None
