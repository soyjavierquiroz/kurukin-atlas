"""Pure fixture tests for asset_search_v1 and asset_candidates_v1."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.contracts.asset_candidates_v1 import SelectedRenditionV1
from app.contracts.asset_search_v1 import AssetSearchV1
from app.search.errors import SearchPoolCapacityError
from app.search.repository import SqlAlchemySearchRepository
from app.search.service import (
    SearchCandidateEvidence, SearchRenditionEvidence, _state_for_terms,
    normalize_duration_seconds, search_assets,
)


class Repository:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def fetch_candidates(self, scope, *, pool_limit):
        self.calls.append((scope, pool_limit))
        return self.rows


def item(uid, *, scope="general", title_id=None, brand_id=None, renditions=("horizontal",), **values):
    overrides = values.pop("overrides", {})
    durations = values.pop("durations", {})
    return SearchCandidateEvidence(
        asset_uid=uid, producer="mbe", source_kind="movie", source_key="source", producer_asset_id=f"producer-{uid}", catalog_scope=scope,
        title_id=title_id, brand_id=brand_id,
        renditions={
            kind: SearchRenditionEvidence(kind, kind == "horizontal", overrides.get(kind, {}), durations.get(kind))
            for kind in renditions
        },
        **values,
    )


def request(scope, **kwargs):
    return AssetSearchV1.model_validate({"schema_version": "asset_search_v1", "scope": scope, **kwargs})


def result(rows, scope={"kind": "general"}, **kwargs):
    return search_assets(Repository(rows), request(scope, **kwargs))


def test_scope_is_hard_for_every_frozen_kind():
    rows = [item("t1", scope="title", title_id="one"), item("t2", scope="title", title_id="two"),
            item("b", scope="brand", brand_id="brand"), item("g")]
    assert [x.asset_uid for x in result(rows, {"kind": "title", "title_id": "one"}).candidates] == ["t1"]
    assert [x.asset_uid for x in result(rows, {"kind": "titles", "title_ids": ["two"]}).candidates] == ["t2"]
    assert {x.asset_uid for x in result(rows, {"kind": "all_titles"}).candidates} == {"t1", "t2"}
    assert [x.asset_uid for x in result(rows, {"kind": "brand", "brand_id": "brand"}).candidates] == ["b"]
    assert [x.asset_uid for x in result(rows).candidates] == ["g"]
    assert {x.asset_uid for x in result(rows, {"kind": "catalog"}).candidates} == {"t1", "t2", "b", "g"}


@pytest.mark.parametrize("scope", [
    {"kind": "title"}, {"kind": "title", "title_id": "x", "brand_id": "b"},
    {"kind": "titles", "title_ids": []}, {"kind": "all_titles", "title_id": "x"},
    {"kind": "brand"}, {"kind": "general", "brand_id": "b"},
])
def test_malformed_scope_is_rejected(scope):
    with pytest.raises(ValidationError):
        request(scope)


def test_query_is_intent_not_evidence_and_unknown_is_distinct():
    row = item("a", visual_summary="happy couple", people={})
    excluded = result([row], query="happy couple", requirements={"people_count_min": 2})
    assert excluded.scarcity_reason == "no_requirement_match"
    allowed = result([row], query="happy couple", requirements={"people_count_min": 2}, unknown_policy="allow", minimum_confidence="any_eligible")
    assert allowed.candidates[0].requirement_matches["people_count"].state == "UNKNOWN"
    assert allowed.candidates[0].confidence == "uncertain"


def test_open_semantic_absence_is_unknown_and_unknown_policy_controls_scarcity():
    row = item("dog", subjects=["dog"])
    excluded = result([row], requirements={"subjects": ["couple"]})
    assert excluded.scarcity_reason == "no_requirement_match"

    returned = result([row], requirements={"subjects": ["couple"]}, unknown_policy="allow", minimum_confidence="any_eligible")
    assert returned.candidates[0].requirement_matches["subjects"].state == "UNKNOWN"
    assert returned.candidates[0].confidence == "uncertain"

    high_only = result([row], requirements={"subjects": ["couple"]}, unknown_policy="allow", minimum_confidence="high")
    assert high_only.scarcity_reason == "no_high_confidence_match"


def test_explicit_negative_is_mismatch_but_non_support_is_not():
    assert _state_for_terms(["couple"], ["dog"], set()).state == "UNKNOWN"
    assert _state_for_terms(["couple"], ["dog"], {"couple"}).state == "MISMATCH"


@pytest.mark.parametrize(
    ("people", "expected"),
    [({"people_count": 1}, "MISMATCH"), ({"people_count": 2}, "MATCH"), ({}, "UNKNOWN")],
)
def test_people_count_is_closed_numeric_evidence(people, expected):
    found = result([item("a", people=people)], requirements={"people_count_min": 2}, unknown_policy="allow", minimum_confidence="any_eligible")
    if expected == "MISMATCH":
        assert found.scarcity_reason == "no_requirement_match"
    else:
        assert found.candidates[0].requirement_matches["people_count"].state == expected


def test_hard_match_mismatch_preferences_and_negative_contradiction():
    rows = [
        item("match", subjects=["couple"], negative_use_cases=["violence"]),
        item("miss", subjects=["dog"]),
    ]
    found = result(rows, requirements={"subjects": ["couple"]}, preferences={"preferred_rendition_kind": "vertical"})
    assert [candidate.asset_uid for candidate in found.candidates] == ["match"]
    assert found.candidates[0].requirement_matches["subjects"].state == "MATCH"
    contradictory = result([rows[0]], requirements={"semantic_terms": ["violence"]})
    assert contradictory.scarcity_reason == "no_requirement_match"


def test_rendition_selection_and_override_are_selected_semantics():
    row = item("a", renditions=("vertical",), visible_emotions=["neutral"], overrides={"vertical": {"visible_emotions": ["happy"]}})
    found = result([row], requirements={"rendition": {"preferred_kind": "horizontal", "acceptable_kinds": ["horizontal", "vertical"]}, "visible_emotions": ["happy"]})
    candidate = found.candidates[0]
    assert candidate.selected_rendition.kind == "vertical"
    assert candidate.selected_rendition.thumbnail_locator is None
    assert candidate.requirement_matches["visible_emotions"].state == "MATCH"
    unavailable = result([row], requirements={"rendition": {"acceptable_kinds": ["horizontal"]}})
    assert unavailable.scarcity_reason == "no_acceptable_rendition"


def test_rendition_preference_never_excludes_another_acceptable_hard_match():
    row = item("a", renditions=("horizontal", "vertical"), visible_emotions=["happy"],
               overrides={"vertical": {"visible_emotions": ["neutral"]}})
    found = result([row], requirements={"visible_emotions": ["happy"]}, preferences={"preferred_rendition_kind": "vertical"})
    assert found.candidates[0].selected_rendition.kind == "horizontal"


def test_ranking_is_lexical_deterministic_and_ignores_technical_quality():
    rows = [item("z", visual_summary="quiet room"), item("a", visual_summary="happy couple in room"), item("b", visual_summary="happy couple in room")]
    found = result(rows, query="happy couple", scope={"kind": "catalog"}, minimum_confidence="any_eligible")
    assert [candidate.asset_uid for candidate in found.candidates] == ["a", "b", "z"]
    assert found.candidates[0].score_components.lexical_relevance > found.candidates[-1].score_components.lexical_relevance


def test_limit_scarcity_and_public_identity():
    rows = [item("a"), item("b")]
    limited = result(rows, limit=1)
    assert len(limited.candidates) == 1
    candidate = limited.candidates[0]
    assert candidate.asset_uid == "a" and candidate.selected_rendition.kind == "horizontal"
    assert candidate.selected_rendition.thumbnail_locator is not None
    assert candidate.source_provenance.producer_asset_id == "producer-a"
    assert "drive" not in candidate.model_dump_json().lower()
    assert result([], scope={"kind": "catalog"}).scarcity_reason == "no_candidates_in_scope"
    assert result(rows, requirements={"subjects": ["absent"]}).scarcity_reason == "no_requirement_match"
    assert result(rows, query="absent").scarcity_reason == "no_high_confidence_match"


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (12.5, 12.5), (None, None), (0, None), (-1, None),
        (float("nan"), None), (float("inf"), None), (float("-inf"), None),
        (True, None), (False, None), ("12.5", None),
    ],
)
def test_public_duration_normalization(stored, expected):
    assert normalize_duration_seconds(stored) == expected


def test_selected_rendition_duration_is_additive_and_selected_rendition_specific():
    legacy = SelectedRenditionV1(kind="horizontal", content_locator="/content")
    assert legacy.duration_seconds is None
    assert SelectedRenditionV1(kind="horizontal", content_locator="/content", duration_seconds=True).duration_seconds is None

    row = item("a", renditions=("horizontal", "vertical"), durations={"horizontal": 12.5, "vertical": 7.25})
    horizontal = result([row], preferences={"preferred_rendition_kind": "horizontal"}).candidates[0]
    vertical = result([row], preferences={"preferred_rendition_kind": "vertical"}).candidates[0]

    assert horizontal.selected_rendition.kind == "horizontal"
    assert horizontal.selected_rendition.duration_seconds == 12.5
    assert vertical.selected_rendition.kind == "vertical"
    assert vertical.selected_rendition.duration_seconds == 7.25
    assert horizontal.asset_uid == vertical.asset_uid == "a"
    assert horizontal.selected_rendition.content_locator.endswith("/horizontal/content")
    assert vertical.selected_rendition.content_locator.endswith("/vertical/content")
    assert horizontal.selected_rendition.thumbnail_locator is not None
    assert vertical.selected_rendition.thumbnail_locator is None


def test_duration_does_not_affect_candidate_ordering_or_ranking():
    rows = [item("a", visual_summary="happy couple", durations={"horizontal": 1}), item("b", visual_summary="happy couple", durations={"horizontal": 99})]
    found = result(rows, query="happy couple", minimum_confidence="any_eligible")
    assert [candidate.asset_uid for candidate in found.candidates] == ["a", "b"]
    assert found.candidates[0].atlas_local_score == found.candidates[1].atlas_local_score


class FakeScalarResult:
    def __init__(self, assets):
        self.assets = assets

    def all(self):
        return self.assets


class FakeSqlSession:
    def __init__(self, assets):
        self.assets = assets
        self.statement = None

    def scalars(self, statement):
        self.statement = statement
        return FakeScalarResult(self.assets)


def sql_asset(uid):
    semantics = SimpleNamespace(
        visual_summary=None, keywords=[], search_terms=[], subjects=[], objects=[], actions=[], setting=None,
        visible_emotions=[], relationships_json=[], people_json={}, negative_use_cases=[], standalone_meaning=None,
        editorial_json={},
    )
    return SimpleNamespace(
        asset_uid=uid, producer="mbe", producer_asset_id=f"producer-{uid}", source_kind="movie", source_key="source",
        catalog_scope="general", title_id=None, brand_id=None, semantics=semantics, renditions=[],
    )


def test_sql_repository_accepts_exact_pool_limit_and_preserves_provenance():
    session = FakeSqlSession([sql_asset("a"), sql_asset("b")])
    rows = SqlAlchemySearchRepository(session).fetch_candidates(request({"kind": "general"}).scope, pool_limit=2)
    assert [row.asset_uid for row in rows] == ["a", "b"]
    assert rows[0].producer_asset_id == "producer-a"
    assert session.statement._limit_clause.value == 3


def test_sql_repository_raises_on_pool_limit_plus_one_without_partial_rows():
    session = FakeSqlSession([sql_asset("a"), sql_asset("b"), sql_asset("c")])
    with pytest.raises(SearchPoolCapacityError):
        search_assets(SqlAlchemySearchRepository(session), request({"kind": "general"}), pool_limit=2)


def test_sql_repository_uses_stored_rendition_duration():
    stored_rendition = SimpleNamespace(
        kind="horizontal", thumbnail_uri=None, semantic_overrides={}, duration_seconds=8.5,
    )
    stored_asset = sql_asset("a")
    stored_asset.renditions = [stored_rendition]
    rows = SqlAlchemySearchRepository(FakeSqlSession([stored_asset])).fetch_candidates(
        request({"kind": "general"}).scope, pool_limit=1,
    )
    assert rows[0].renditions["horizontal"].duration_seconds == 8.5
