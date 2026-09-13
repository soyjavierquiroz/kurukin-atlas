"""Authoritative, read-only catalog lookup for HTTP object delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import LogicalAsset


@dataclass(frozen=True)
class DeliveryRendition:
    kind: str
    storage_uri: str
    thumbnail_uri: str | None
    sha256: str
    size_bytes: int | None
    mime_type: str | None
    thumbnail_sha256: str | None = None
    thumbnail_size_bytes: int | None = None


@dataclass(frozen=True)
class DeliveryAsset:
    asset_uid: UUID
    status: str
    renditions: dict[str, DeliveryRendition]


class DeliveryRepository(Protocol):
    def get_asset(self, asset_uid: UUID) -> DeliveryAsset | None: ...


class SqlAlchemyDeliveryRepository:
    """Reads catalog rows only; physical locators are never inferred or listed."""

    def __init__(self, session: Session):
        self.session = session

    def get_asset(self, asset_uid: UUID) -> DeliveryAsset | None:
        statement = (
            select(LogicalAsset)
            .where(LogicalAsset.asset_uid == asset_uid)
            .options(selectinload(LogicalAsset.renditions))
        )
        asset = self.session.scalar(statement)
        if asset is None:
            return None
        renditions = {
            rendition.kind: DeliveryRendition(
                kind=rendition.kind,
                storage_uri=rendition.storage_uri,
                thumbnail_uri=rendition.thumbnail_uri,
                sha256=rendition.sha256,
                size_bytes=rendition.size_bytes,
                mime_type=rendition.mime_type,
                thumbnail_sha256=rendition.thumbnail_sha256,
                thumbnail_size_bytes=rendition.thumbnail_size_bytes,
            )
            for rendition in asset.renditions
        }
        return DeliveryAsset(asset_uid=asset.asset_uid, status=asset.status, renditions=renditions)
