"""Pure, deterministic Atlas candidate matching and ranking.

The repository supplies bounded, scope-filtered catalog evidence.  This module
does not know about HTTP, storage delivery, or any external provider.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from app.contracts.asset_candidates_v1 import (
    AssetCandidateV1, AssetCandidatesV1, CatalogPlacementV1, ProducerEditorialEvidenceV1,
    RequirementMatchV1, ScoreComponentsV1, SelectedRenditionV1, SourceProvenanceV1,
)
from app.contracts.asset_search_v1 import AssetSearchV1, RenditionKind, SearchScopeV1


_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def normalized_tokens(value: Any) -> set[str]:
    """Conservative Unicode-aware case-folded tokenization for stored text."""

    if not isinstance(value, str):
        return set()
    return set(_TOKEN.findall(unicodedata.normalize("NFKC", value).casefold()))


def _text_values(value: Any) -> list[str]:
    """Flatten explicit producer/editorial values without inventing semantic claims."""

    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _text_values(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _text_values(child)]
    return []


def _terms(value: Any) -> set[str]:
    return set().union(*(normalized_tokens(item) for item in _text_values(value))) if _text_values(value) else set()


@dataclass(frozen=True)
class SearchRenditionEvidence:
    kind: RenditionKind
    thumbnail_present: bool = False
    semantic_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchCandidateEvidence:
    """Database-independent catalog row used by the matcher and fake repositories."""

    asset_uid: str
    producer: str
    source_kind: str
    source_key: str
    producer_asset_id: str
    catalog_scope: str
    title_id: str | None = None
    brand_id: str | None = None
    visual_summary: str | None = None
    keywords: list[Any] = field(default_factory=list)
    search_terms: list[Any] = field(default_factory=list)
    subjects: list[Any] = field(default_factory=list)
    objects: list[Any] = field(default_factory=list)
    actions: list[Any] = field(default_factory=list)
    setting: str | None = None
    visible_emotions: list[Any] = field(default_factory=list)
    relationships: list[Any] = field(default_factory=list)
    people: dict[str, Any] = field(default_factory=dict)
    negative_use_cases: list[Any] = field(default_factory=list)
    standalone_meaning: str | None = None
    editorial: dict[str, Any] = field(default_factory=dict)
    renditions: dict[RenditionKind, SearchRenditionEvidence] = field(default_factory=dict)


class SearchRepository(Protocol):
    """Retrieves an authoritative bounded scope pool or raises capacity error.

    Implementations must apply ``scope`` before retrieval and must not silently
    truncate a scope that exceeds ``pool_limit``.
    """

    def fetch_candidates(self, scope: SearchScopeV1, *, pool_limit: int) -> Sequence[SearchCandidateEvidence]: ...


def _effective(candidate: SearchCandidateEvidence, rendition: SearchRenditionEvidence) -> dict[str, Any]:
    values = {
        "visual_summary": candidate.visual_summary, "keywords": candidate.keywords, "search_terms": candidate.search_terms,
        "subjects": candidate.subjects, "objects": candidate.objects, "actions": candidate.actions,
        "setting": candidate.setting, "visible_emotions": candidate.visible_emotions,
        "relationships": candidate.relationships, "people": candidate.people,
        "negative_use_cases": candidate.negative_use_cases, "standalone_meaning": candidate.standalone_meaning,
        "editorial": candidate.editorial,
    }
    # An override wins only for a known field it explicitly supplies.
    for name, value in rendition.semantic_overrides.items():
        if name in values:
            values[name] = value
    return values


def _state_for_terms(requested: list[str], stored: Any, negatives: set[str]) -> RequirementMatchV1:
    request_terms = set().union(*(normalized_tokens(term) for term in requested)) if requested else set()
    if not request_terms:
        return RequirementMatchV1(state="MATCH", detail="not requested")
    if request_terms & negatives:
        return RequirementMatchV1(state="MISMATCH", evidence=sorted(request_terms & negatives), detail="explicit negative use-case evidence")
    evidence = _terms(stored)
    if not evidence:
        return RequirementMatchV1(state="UNKNOWN", detail="stored evidence unavailable")
    found = request_terms & evidence
    if found == request_terms:
        return RequirementMatchV1(state="MATCH", evidence=sorted(found))
    return RequirementMatchV1(
        state="UNKNOWN", evidence=sorted(found),
        detail="stored evidence does not establish every requested claim",
    )


def _people_match(request: AssetSearchV1, values: dict[str, Any], negatives: set[str]) -> dict[str, RequirementMatchV1]:
    people = values["people"] if isinstance(values["people"], dict) else {}
    result: dict[str, RequirementMatchV1] = {}
    presentations = request.requirements.people_presentations
    if presentations:
        # This is producer/editorial apparent-presentation metadata only.  It
        # deliberately includes no identity or biological inference.
        result["people_presentations"] = _state_for_terms(presentations, people, negatives)
    if request.requirements.people_count_min is not None or request.requirements.people_count_max is not None:
        count = people.get("people_count")
        if not isinstance(count, int) or isinstance(count, bool):
            result["people_count"] = RequirementMatchV1(state="UNKNOWN", detail="stored people count unavailable")
        elif ((request.requirements.people_count_min is not None and count < request.requirements.people_count_min) or
              (request.requirements.people_count_max is not None and count > request.requirements.people_count_max)):
            result["people_count"] = RequirementMatchV1(state="MISMATCH", evidence=[str(count)])
        else:
            result["people_count"] = RequirementMatchV1(state="MATCH", evidence=[str(count)])
    return result


def _matches(request: AssetSearchV1, values: dict[str, Any], rendition: SearchRenditionEvidence) -> dict[str, RequirementMatchV1]:
    negatives = _terms(values["negative_use_cases"])
    requirements = request.requirements
    result: dict[str, RequirementMatchV1] = {
        "rendition": RequirementMatchV1(state="MATCH", evidence=[rendition.kind]),
        "relationships": _state_for_terms(requirements.relationships, values["relationships"], negatives),
        "visible_emotions": _state_for_terms(requirements.visible_emotions, values["visible_emotions"], negatives),
        "setting": _state_for_terms(requirements.setting, values["setting"], negatives),
        "subjects": _state_for_terms(requirements.subjects, values["subjects"], negatives),
        "objects": _state_for_terms(requirements.objects, values["objects"], negatives),
        "actions": _state_for_terms(requirements.actions, values["actions"], negatives),
    }
    semantic_evidence = [values[name] for name in ("visual_summary", "keywords", "search_terms", "subjects", "objects", "actions", "setting", "visible_emotions", "relationships", "standalone_meaning")]
    result["semantic_terms"] = _state_for_terms(requirements.semantic_terms, semantic_evidence, negatives)
    result.update(_people_match(request, values, negatives))
    return result


def _requested_hard_keys(request: AssetSearchV1) -> set[str]:
    r = request.requirements
    keys = {"rendition"}
    for key in ("relationships", "visible_emotions", "setting", "subjects", "objects", "actions", "semantic_terms", "people_presentations"):
        if getattr(r, key):
            keys.add(key)
    if r.people_count_min is not None or r.people_count_max is not None:
        keys.add("people_count")
    return keys


def _ordered_renditions(candidate: SearchCandidateEvidence, request: AssetSearchV1) -> list[SearchRenditionEvidence]:
    """Return acceptable choices with a preferred kind first, never as exclusion."""

    requirement = request.requirements.rendition
    allowed = [candidate.renditions[kind] for kind in requirement.acceptable_kinds if kind in candidate.renditions]
    if requirement.preferred_required:
        preferred = candidate.renditions.get(requirement.preferred_kind)  # type: ignore[arg-type]
        return [preferred] if preferred is not None else []
    preferred = requirement.preferred_kind or request.preferences.preferred_rendition_kind
    if preferred in candidate.renditions and preferred in requirement.acceptable_kinds:
        preferred_rendition = candidate.renditions[preferred]  # type: ignore[index]
        return [preferred_rendition, *(item for item in allowed if item.kind != preferred)]
    return allowed


def _in_scope(candidate: SearchCandidateEvidence, scope: SearchScopeV1) -> bool:
    """Defensive second enforcement for fake/custom repositories."""

    if scope.kind == "title":
        return candidate.catalog_scope == "title" and candidate.title_id == scope.title_id
    if scope.kind == "titles":
        return candidate.catalog_scope == "title" and candidate.title_id in scope.title_ids
    if scope.kind == "all_titles":
        return candidate.catalog_scope == "title"
    if scope.kind == "brand":
        return candidate.catalog_scope == "brand" and candidate.brand_id == scope.brand_id
    if scope.kind == "general":
        return candidate.catalog_scope == "general"
    return candidate.catalog_scope in {"title", "brand", "general"}


def _lexical_score(query: str, values: dict[str, Any]) -> float:
    query_tokens = normalized_tokens(query)
    if not query_tokens:
        return 0.0
    # Editorial/visual fields carry meaning; technical fields intentionally do not contribute.
    weighted = (("visual_summary", 5.0), ("search_terms", 4.0), ("keywords", 3.0), ("subjects", 3.0),
                ("objects", 3.0), ("actions", 3.0), ("setting", 2.0), ("visible_emotions", 2.0),
                ("relationships", 2.0), ("standalone_meaning", 4.0))
    score = 0.0
    for name, weight in weighted:
        overlap = query_tokens & _terms(values[name])
        score += weight * len(overlap) / len(query_tokens)
    return round(score, 6)


def _candidate(candidate: SearchCandidateEvidence, request: AssetSearchV1, rendition: SearchRenditionEvidence) -> tuple[AssetCandidateV1, bool]:
    values = _effective(candidate, rendition)
    matches = _matches(request, values, rendition)
    hard = _requested_hard_keys(request)
    unknowns = [key for key in hard if matches[key].state == "UNKNOWN"]
    structured = sum(matches[key].state == "MATCH" for key in hard) / len(hard)
    lexical = _lexical_score(request.query, values)
    rendition_preference = 1.0 if rendition.kind == (request.requirements.rendition.preferred_kind or request.preferences.preferred_rendition_kind) else 0.0
    editorial_signal = 1.0 if values["standalone_meaning"] or values["search_terms"] else 0.0
    total = round(lexical * 10.0 + structured * 4.0 + rendition_preference * 0.25 + editorial_signal * 0.1, 6)
    high = not unknowns and (not normalized_tokens(request.query) or lexical > 0 or len(hard) > 1)
    # Query text is intent, not a contradiction claim. Only hard requirements
    # may make explicit negative producer/editorial evidence applicable.
    contradictions = sorted(_terms(values["negative_use_cases"]) & set().union(*(
        normalized_tokens(term) for terms in (request.requirements.relationships, request.requirements.visible_emotions,
        request.requirements.setting, request.requirements.subjects, request.requirements.objects, request.requirements.actions,
        request.requirements.semantic_terms, request.requirements.people_presentations) for term in terms
    )))
    evidence = ProducerEditorialEvidenceV1(**values, rendition_override=rendition.semantic_overrides)
    result = AssetCandidateV1(
        asset_uid=candidate.asset_uid, producer=candidate.producer,
        source_provenance=SourceProvenanceV1(
            source_kind=candidate.source_kind, source_key=candidate.source_key,
            producer_asset_id=candidate.producer_asset_id,
        ),
        catalog_placement=CatalogPlacementV1(catalog_scope=candidate.catalog_scope, title_id=candidate.title_id, brand_id=candidate.brand_id),
        selected_rendition=SelectedRenditionV1(
            kind=rendition.kind, content_locator=f"/v1/assets/{candidate.asset_uid}/renditions/{rendition.kind}/content",
            thumbnail_locator=(f"/v1/assets/{candidate.asset_uid}/renditions/{rendition.kind}/thumbnail" if rendition.thumbnail_present else None),
        ), evidence=evidence, requirement_matches=matches, contradictions=contradictions,
        confidence="high" if high else "uncertain",
        score_components=ScoreComponentsV1(lexical_relevance=lexical, structured_match=round(structured, 6),
                                             rendition_preference=rendition_preference, editorial_signal=editorial_signal),
        atlas_local_score=total,
    )
    return result, high


def search_assets(repository: SearchRepository, request: AssetSearchV1, *, pool_limit: int = 400) -> AssetCandidatesV1:
    """Search one provider-local catalog pool; ordinary scarcity is a valid result.

    ``pool_limit`` is an internal cap, not a public result target.  Repositories
    must apply the hard scope filter before returning the pool and raise
    ``SearchPoolCapacityError`` rather than silently truncate an over-capacity
    scoped result.
    """

    if pool_limit < 1:
        raise ValueError("pool_limit must be positive")
    scoped = [item for item in repository.fetch_candidates(request.scope, pool_limit=pool_limit) if _in_scope(item, request.scope)]
    if not scoped:
        return AssetCandidatesV1(scarcity_reason="no_candidates_in_scope")
    rendition_ready: list[tuple[SearchCandidateEvidence, list[SearchRenditionEvidence]]] = []
    for item in scoped:
        choices = _ordered_renditions(item, request)
        if choices:
            rendition_ready.append((item, choices))
    if not rendition_ready:
        return AssetCandidatesV1(scarcity_reason="no_acceptable_rendition")
    eligible: list[tuple[AssetCandidateV1, bool]] = []
    for item, choices in rendition_ready:
        eligible_choices: list[tuple[AssetCandidateV1, bool]] = []
        for rendition in choices:
            candidate, high = _candidate(item, request, rendition)
            states = [match.state for match in candidate.requirement_matches.values()]
            if "MISMATCH" not in states and (request.unknown_policy == "allow" or "UNKNOWN" not in states):
                eligible_choices.append((candidate, high))
        if eligible_choices:
            # Confidence protects hard semantics before a rendition preference;
            # score then preserves deterministic preferred-kind ranking.
            best_confidence = max(value[1] for value in eligible_choices)
            confidence_choices = [value for value in eligible_choices if value[1] == best_confidence]
            best_score = max(value[0].atlas_local_score for value in confidence_choices)
            eligible.append(next(value for value in confidence_choices if value[0].atlas_local_score == best_score))
    if not eligible:
        return AssetCandidatesV1(scarcity_reason="no_requirement_match")
    selected = eligible if request.minimum_confidence == "any_eligible" else [item for item in eligible if item[1]]
    if not selected:
        return AssetCandidatesV1(scarcity_reason="no_high_confidence_match")
    ranked = sorted((item[0] for item in selected), key=lambda item: (-item.atlas_local_score, item.asset_uid))
    return AssetCandidatesV1(candidates=ranked[:request.limit])
