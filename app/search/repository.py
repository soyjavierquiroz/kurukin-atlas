"""SQLAlchemy retrieval adapter for the Atlas local search core."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, selectinload

from app.contracts.asset_search_v1 import SearchScopeV1
from app.models import LogicalAsset
from app.search.errors import SearchPoolCapacityError
from app.search.service import SearchCandidateEvidence, SearchRenditionEvidence


class SqlAlchemySearchRepository:
    """Loads a scoped pool in bounded eager queries without partial results."""

    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _scoped(statement: Select[tuple[LogicalAsset]], scope: SearchScopeV1) -> Select[tuple[LogicalAsset]]:
        if scope.kind == "title":
            return statement.where(LogicalAsset.catalog_scope == "title", LogicalAsset.title_id == scope.title_id)
        if scope.kind == "titles":
            return statement.where(LogicalAsset.catalog_scope == "title", LogicalAsset.title_id.in_(scope.title_ids))
        if scope.kind == "all_titles":
            return statement.where(LogicalAsset.catalog_scope == "title")
        if scope.kind == "brand":
            return statement.where(LogicalAsset.catalog_scope == "brand", LogicalAsset.brand_id == scope.brand_id)
        if scope.kind == "general":
            return statement.where(LogicalAsset.catalog_scope == "general")
        return statement.where(LogicalAsset.catalog_scope.in_(("title", "brand", "general")))

    def fetch_candidates(self, scope: SearchScopeV1, *, pool_limit: int) -> Sequence[SearchCandidateEvidence]:
        if pool_limit < 1:
            raise ValueError("pool_limit must be positive")
        statement = select(LogicalAsset).where(LogicalAsset.status == "active").options(
            selectinload(LogicalAsset.semantics), selectinload(LogicalAsset.renditions),
        ).order_by(LogicalAsset.asset_uid).limit(pool_limit + 1)
        assets = self.session.scalars(self._scoped(statement, scope)).all()
        if len(assets) > pool_limit:
            raise SearchPoolCapacityError(
                f"scoped active asset count exceeds authoritative pool limit {pool_limit}"
            )
        rows: list[SearchCandidateEvidence] = []
        for asset in assets:
            semantics = asset.semantics
            renditions = {
                rendition.kind: SearchRenditionEvidence(
                    rendition.kind,
                    rendition.thumbnail_uri is not None,
                    rendition.semantic_overrides or {},
                    rendition.duration_seconds,
                )
                for rendition in asset.renditions
            }
            rows.append(SearchCandidateEvidence(
                asset_uid=str(asset.asset_uid), producer=asset.producer, source_kind=asset.source_kind, source_key=asset.source_key,
                producer_asset_id=asset.producer_asset_id,
                catalog_scope=asset.catalog_scope, title_id=asset.title_id, brand_id=asset.brand_id,
                visual_summary=semantics.visual_summary if semantics else None,
                keywords=semantics.keywords or [] if semantics else [], search_terms=semantics.search_terms or [] if semantics else [],
                subjects=semantics.subjects or [] if semantics else [], objects=semantics.objects or [] if semantics else [],
                actions=semantics.actions or [] if semantics else [], setting=semantics.setting if semantics else None,
                visible_emotions=semantics.visible_emotions or [] if semantics else [], relationships=semantics.relationships_json or [] if semantics else [],
                people=semantics.people_json or {} if semantics else {}, negative_use_cases=semantics.negative_use_cases or [] if semantics else [],
                standalone_meaning=semantics.standalone_meaning if semantics else None,
                editorial=semantics.editorial_json or {} if semantics else {}, renditions=renditions,
            ))
        return rows
