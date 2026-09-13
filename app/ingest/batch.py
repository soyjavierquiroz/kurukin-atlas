"""Sequential, bounded application primitive for MBE outbox ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.contracts.asset_metadata_v1 import AssetMetadataV1
from app.ingest.catalog import CatalogPlacement
from app.ingest.discovery import DiscoveredPackage, DiscoveryResult, DiscoveryStatus, discover_mbe_packages
from app.ingest.orchestrator import IngestResult, IngestStatus, ingest_package
from app.storage.backend import StorageBackend


PlacementResolver = Callable[[AssetMetadataV1], CatalogPlacement]
IngestCallable = Callable[[str | Path, CatalogPlacement, StorageBackend, sessionmaker[Session]], IngestResult]


class BatchPackageStatus(StrEnum):
    """Batch handling outcome, distinct from source discovery classification."""

    READY = "READY"  # discovered READY but unattempted after limit or an error halt
    NOT_READY = "NOT_READY"
    INVALID = "INVALID"
    DRY_RUN = "DRY_RUN"
    CATALOGED = "CATALOGED"
    INGEST_NOT_READY = "INGEST_NOT_READY"
    FAILED = "FAILED"


@dataclass(frozen=True)
class BatchPackageResult:
    package: DiscoveredPackage
    status: BatchPackageStatus
    asset_uid: UUID | None = None
    error_category: str | None = None
    error_message: str | None = None
    placement: CatalogPlacement | None = None


@dataclass(frozen=True)
class BatchSummary:
    """Structured source classifications plus per-package batch outcomes."""

    discovery: DiscoveryResult
    packages: tuple[BatchPackageResult, ...]

    @property
    def discovered_json(self) -> int:
        return self.discovery.discovered_json

    @property
    def ready(self) -> int:
        """READY at discovery, including packages beyond a configured limit."""
        return self.discovery.ready

    @property
    def not_ready(self) -> int:
        """NOT_READY at discovery; ingest races are reported separately below."""
        return self.discovery.not_ready

    @property
    def invalid(self) -> int:
        return self.discovery.invalid

    @property
    def attempted(self) -> int:
        return sum(item.status in {BatchPackageStatus.CATALOGED,
                                   BatchPackageStatus.INGEST_NOT_READY,
                                   BatchPackageStatus.FAILED}
                   and not (item.error_category == "PlacementResolverError")
                   for item in self.packages)

    @property
    def cataloged(self) -> int:
        return sum(item.status == BatchPackageStatus.CATALOGED for item in self.packages)

    @property
    def failed(self) -> int:
        return sum(item.status == BatchPackageStatus.FAILED for item in self.packages)

    @property
    def ingest_not_ready(self) -> int:
        """Packages that changed after discovery and returned NOT_READY at ingest."""
        return sum(item.status == BatchPackageStatus.INGEST_NOT_READY for item in self.packages)


def _source_result(package: DiscoveredPackage) -> BatchPackageResult:
    status = (BatchPackageStatus.NOT_READY if package.status == DiscoveryStatus.NOT_READY
              else BatchPackageStatus.INVALID)
    return BatchPackageResult(package, status, error_message=package.error)


def ingest_mbe_batch(
    outbox: str | Path,
    storage_backend: StorageBackend | None,
    session_factory: sessionmaker[Session] | None,
    placement_resolver: PlacementResolver,
    limit: int | None = None,
    dry_run: bool = False,
    stop_on_error: bool = False,
    *,
    ingest: IngestCallable = ingest_package,
) -> BatchSummary:
    """Discover then sequentially hand selected READY packages to ``ingest_package``.

    ``limit`` bounds selected READY packages, including placement resolution,
    not all discovered JSONs; zero selects none.  Dry runs resolve placement
    for that same selected set but never call the single-package ingest
    function.
    """

    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
        raise ValueError("limit must be a non-negative integer or None")
    if not dry_run and (storage_backend is None or session_factory is None):
        raise ValueError("storage_backend and session_factory are required outside dry-run")
    discovery = discover_mbe_packages(outbox)
    results: list[BatchPackageResult] = []
    selected = 0
    halted = False
    for package in discovery.packages:
        if package.status != DiscoveryStatus.READY:
            results.append(_source_result(package))
            continue
        if halted or (limit is not None and selected >= limit):
            results.append(BatchPackageResult(package, BatchPackageStatus.READY))
            continue
        selected += 1
        assert package.metadata is not None  # READY is produced only after a successful parse.
        try:
            placement = placement_resolver(package.metadata)
            if not isinstance(placement, CatalogPlacement):
                raise TypeError("placement resolver must return CatalogPlacement")
        except Exception as exc:
            results.append(BatchPackageResult(package, BatchPackageStatus.FAILED,
                                              error_category="PlacementResolverError",
                                              error_message=str(exc)))
            if stop_on_error:
                halted = True
            continue
        if dry_run:
            results.append(BatchPackageResult(package, BatchPackageStatus.DRY_RUN,
                                              placement=placement))
            continue
        try:
            # The non-dry-run guard above establishes both runtime dependencies.
            result = ingest(package.json_path, placement, storage_backend, session_factory)  # type: ignore[arg-type]
        except Exception as exc:
            results.append(BatchPackageResult(package, BatchPackageStatus.FAILED,
                                              error_category=type(exc).__name__,
                                              error_message=str(exc), placement=placement))
            if stop_on_error:
                halted = True
            continue
        if result.status == IngestStatus.CATALOGED:
            results.append(BatchPackageResult(package, BatchPackageStatus.CATALOGED,
                                              asset_uid=result.asset_uid, placement=placement))
        elif result.status == IngestStatus.NOT_READY:
            results.append(BatchPackageResult(package, BatchPackageStatus.INGEST_NOT_READY,
                                              asset_uid=result.asset_uid, placement=placement))
        else:  # Defensive if the single-package API gains a status without batch handling.
            results.append(BatchPackageResult(package, BatchPackageStatus.FAILED,
                                              asset_uid=result.asset_uid,
                                              error_category="UnexpectedIngestStatus",
                                              error_message=str(result.status), placement=placement))
            if stop_on_error:
                halted = True
    return BatchSummary(discovery, tuple(results))
