"""Versioned, HTTP-independent request contract for Atlas local search."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


RenditionKind = Literal["horizontal", "vertical"]
ScopeKind = Literal["title", "titles", "all_titles", "brand", "general", "catalog"]


class SearchScopeV1(BaseModel):
    """A catalog placement filter. Scope is deliberately never an expansion hint."""

    model_config = ConfigDict(extra="forbid")

    kind: ScopeKind
    title_id: str | None = None
    title_ids: list[str] = Field(default_factory=list)
    brand_id: str | None = None

    @model_validator(mode="after")
    def validate_combination(self) -> "SearchScopeV1":
        single_title = self.kind == "title"
        multiple_titles = self.kind == "titles"
        single_brand = self.kind == "brand"
        if single_title != (self.title_id is not None):
            raise ValueError("title scope requires exactly one title_id")
        if multiple_titles != bool(self.title_ids):
            raise ValueError("titles scope requires a non-empty title_ids list")
        if single_brand != (self.brand_id is not None):
            raise ValueError("brand scope requires exactly one brand_id")
        if not single_title and self.title_id is not None:
            raise ValueError("title_id is valid only for title scope")
        if not multiple_titles and self.title_ids:
            raise ValueError("title_ids is valid only for titles scope")
        if not single_brand and self.brand_id is not None:
            raise ValueError("brand_id is valid only for brand scope")
        if self.title_id is not None and not self.title_id.strip():
            raise ValueError("title_id must not be blank")
        if self.brand_id is not None and not self.brand_id.strip():
            raise ValueError("brand_id must not be blank")
        if any(not item.strip() for item in self.title_ids) or len(set(self.title_ids)) != len(self.title_ids):
            raise ValueError("title_ids must be non-blank and unique")
        return self


class RenditionRequirementV1(BaseModel):
    """Availability is hard; choosing a preferred available kind is normally soft."""

    model_config = ConfigDict(extra="forbid")

    acceptable_kinds: list[RenditionKind] = Field(default_factory=lambda: ["horizontal", "vertical"])
    preferred_kind: RenditionKind | None = None
    preferred_required: bool = False

    @model_validator(mode="after")
    def validate_kinds(self) -> "RenditionRequirementV1":
        if not self.acceptable_kinds or len(set(self.acceptable_kinds)) != len(self.acceptable_kinds):
            raise ValueError("acceptable_kinds must be a non-empty unique list")
        if self.preferred_kind is not None and self.preferred_kind not in self.acceptable_kinds:
            raise ValueError("preferred_kind must be acceptable")
        if self.preferred_required and self.preferred_kind is None:
            raise ValueError("preferred_required requires preferred_kind")
        return self


class AssetRequirementsV1(BaseModel):
    """Hard requirements evaluated only against stored producer/editorial evidence."""

    model_config = ConfigDict(extra="forbid")

    rendition: RenditionRequirementV1 = Field(default_factory=RenditionRequirementV1)
    people_presentations: list[str] = Field(default_factory=list)
    people_count_min: int | None = Field(default=None, ge=0)
    people_count_max: int | None = Field(default=None, ge=0)
    relationships: list[str] = Field(default_factory=list)
    visible_emotions: list[str] = Field(default_factory=list)
    setting: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    objects: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    semantic_terms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ranges_and_terms(self) -> "AssetRequirementsV1":
        if self.people_count_min is not None and self.people_count_max is not None and self.people_count_min > self.people_count_max:
            raise ValueError("people_count_min cannot exceed people_count_max")
        for field_name in ("people_presentations", "relationships", "visible_emotions", "setting", "subjects", "objects", "actions", "semantic_terms"):
            values = getattr(self, field_name)
            if any(not value.strip() for value in values):
                raise ValueError(f"{field_name} cannot contain blank terms")
        return self


class AssetPreferencesV1(BaseModel):
    """Soft ranking signals.  They never change eligibility."""

    model_config = ConfigDict(extra="forbid")

    preferred_rendition_kind: RenditionKind | None = None


class AssetSearchV1(BaseModel):
    """Public request boundary for provider-local Atlas search."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["asset_search_v1"]
    query: str = ""
    scope: SearchScopeV1
    requirements: AssetRequirementsV1 = Field(default_factory=AssetRequirementsV1)
    preferences: AssetPreferencesV1 = Field(default_factory=AssetPreferencesV1)
    unknown_policy: Literal["exclude", "allow"] = "exclude"
    minimum_confidence: Literal["high", "any_eligible"] = "high"
    limit: int = Field(default=20, ge=1, le=100)
