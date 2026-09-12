"""Explicit synchronous single-package ingest. NO SOURCE CLEANUP IN P0 MILESTONE #6."""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from app.contracts.asset_metadata_v1 import AssetMetadataV1
from app.ingest.catalog import CatalogIngestRequest, CatalogPlacement, catalog_validated_asset
from app.ingest.fingerprint import observed_package_fingerprint
from app.ingest.normalize import normalize_metadata
from app.ingest.package import (
    PackageReadiness, UnsafePackagePathError, package_readiness,
    safe_package_path, validate_referenced_bytes,
)
from app.ingest.trust import validate_trust
from app.ingest.verification import CatalogVerificationError, verify_cataloged_asset
from app.storage.backend import StorageBackend
from app.storage.errors import StorageIntegrityError


class IngestOrchestrationError(RuntimeError):
    """An ingest boundary validation failed."""


class ProducerMetadataError(IngestOrchestrationError):
    """Producer JSON is not UTF-8 JSON conforming to AssetMetadataV1."""


class PackageValidationError(IngestOrchestrationError):
    """Package paths or referenced bytes are invalid."""


class IngestStatus(StrEnum):
    NOT_READY = "NOT_READY"
    CATALOGED = "CATALOGED"


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    asset_uid: uuid.UUID | None = None
    package_fingerprint: str | None = None
    storage_created: bool | None = None
    logical_asset_created: bool | None = None
    package_changed: bool | None = None
    ingest_state: str | None = None
    catalog_verified: bool = False


def _read_json_source(path: Path) -> bytes:
    """Reuse package containment checks and open only a regular, non-symlink file."""
    try:
        safe_package_path(path.parent, path.name)
        # NONBLOCK avoids waiting on a FIFO substituted before open; fstat rejects it.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise PackageValidationError("JSON source must be a regular file")
            return stream.read()
    except FileNotFoundError:
        raise
    except (UnsafePackagePathError, OSError) as exc:
        raise PackageValidationError("unsafe or unreadable JSON source") from exc


def ingest_package(
    json_path: str | Path,
    placement: CatalogPlacement,
    storage_backend: StorageBackend,
    session_factory: sessionmaker[Session],
    stability_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> IngestResult:
    """Validate, copy/verify, commit catalog, then verify through a new DB session.

    Prior successful stages remain intact on failure. Replay is the recovery
    mechanism; no source cleanup or compensating storage/catalog deletion occurs.
    """
    path = Path(json_path).absolute()
    not_ready = IngestResult(IngestStatus.NOT_READY)
    try:
        source_bytes = _read_json_source(path)
    except FileNotFoundError:
        return not_ready
    try:
        raw = json.loads(source_bytes.decode("utf-8"))
        metadata = AssetMetadataV1.model_validate(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ProducerMetadataError("invalid producer asset_metadata_v1 JSON") from exc

    readiness = package_readiness(metadata, path, stability_seconds, sleep)
    if readiness == PackageReadiness.NOT_READY:
        return not_ready
    if readiness != PackageReadiness.VALID:
        raise PackageValidationError("unsafe package member paths")
    # A JSON replacement during the stability interval must not hand off stale semantics.
    try:
        if _read_json_source(path) != source_bytes:
            return not_ready
    except FileNotFoundError:
        return not_ready
    validate_trust(metadata)
    validation = validate_referenced_bytes(metadata, path.parent)
    if validation.readiness != PackageReadiness.VALID:
        raise PackageValidationError("; ".join(validation.errors))
    asset = normalize_metadata(metadata, raw)
    fingerprint = observed_package_fingerprint(asset)
    stored = storage_backend.store_package(metadata, asset, path, fingerprint)
    if stored.package_fingerprint != fingerprint:
        raise StorageIntegrityError("storage package fingerprint does not match requested fingerprint")
    request = CatalogIngestRequest(
        normalized_asset=asset, placement=placement, verified_storage_manifest=stored.manifest,
        source_base_uri=path.parent.resolve().as_uri(), package_basename=path.stem,
        destination_verified_at=stored.destination_verified_at,
        observed_package_fingerprint=fingerprint,
    )
    with session_factory.begin() as session:
        catalog_result = catalog_validated_asset(session, request)
    # Exiting begin() successfully commits. This factory call creates a NEW Session.
    with session_factory() as verification_session:
        state = verify_cataloged_asset(verification_session, request)
    return IngestResult(
        status=IngestStatus.CATALOGED, asset_uid=asset.asset_uid,
        package_fingerprint=fingerprint, storage_created=stored.created,
        logical_asset_created=catalog_result.logical_asset_created,
        package_changed=catalog_result.package_changed, ingest_state=state,
        catalog_verified=True,
    )
