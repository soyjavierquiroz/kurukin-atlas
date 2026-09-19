"""Operator-facing MBE episode preflight and ingest adapter.

This module intentionally composes the established MBE package boundary.  It
does not introduce another publisher, catalog writer, or producer contract.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.contracts.asset_metadata_v1 import AssetMetadataV1
from app.ingest.catalog import CatalogPlacement
from app.ingest.curated import CuratedProbeError, FFprobeRunner, probe_curated_video
from app.ingest.discovery import DiscoveredPackage, DiscoveryStatus, discover_mbe_packages
from app.ingest.normalize import NormalizedAsset, atlas_asset_uid, normalize_metadata
from app.ingest.orchestrator import IngestResult, ingest_package
from app.ingest.package import PackageReadiness, package_readiness, validate_referenced_bytes
from app.ingest.trust import validate_trust
from app.models import IngestRecord, LogicalAsset
from app.storage.backend import StorageBackend


class MovieBrollEligibilityError(ValueError):
    """Producer editorial state is not eligible for operator publication."""


class MovieBrollTechnicalError(ValueError):
    """A producer video cannot be decoded into required technical facts."""


@dataclass(frozen=True)
class EpisodeIdentity:
    series: str
    season: int
    episode: int
    episode_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.series, str) or not self.series.strip() or self.series != self.series.strip():
            raise ValueError("series must be a non-empty normalized string")
        if not isinstance(self.episode_key, str) or not self.episode_key.strip() or self.episode_key != self.episode_key.strip():
            raise ValueError("episode_key must be a non-empty normalized string")
        if isinstance(self.season, bool) or not isinstance(self.season, int) or self.season < 1:
            raise ValueError("season must be a positive integer")
        if isinstance(self.episode, bool) or not isinstance(self.episode, int) or self.episode < 1:
            raise ValueError("episode must be a positive integer")

    @property
    def provenance(self) -> dict[str, Any]:
        return {
            "series_name": self.series,
            "season_number": self.season,
            "episode_number": self.episode,
            "episode_key": self.episode_key,
        }


@dataclass(frozen=True)
class PreflightItem:
    package: DiscoveredPackage
    asset: NormalizedAsset | None
    eligible: bool
    blocked_reasons: tuple[str, ...]
    existing: bool = False
    reconcile: bool = False


@dataclass(frozen=True)
class PreflightResult:
    assets_dir: Path
    identity: EpisodeIdentity
    items: tuple[PreflightItem, ...]

    @property
    def discovered(self) -> int:
        return len(self.items)

    @property
    def eligible(self) -> int:
        return sum(item.eligible for item in self.items)

    @property
    def existing(self) -> int:
        return sum(item.existing for item in self.items)

    @property
    def reconcile(self) -> int:
        return sum(item.reconcile for item in self.items)

    @property
    def new(self) -> int:
        return sum(item.eligible and not item.existing and not item.reconcile for item in self.items)

    @property
    def blocked(self) -> int:
        return sum(bool(item.blocked_reasons) for item in self.items)


def assets_directory(run: str | Path) -> Path:
    """Resolve the producer's canonical ``run/assets`` package directory."""

    run_path = Path(run).absolute()
    if not run_path.is_dir():
        raise ValueError(f"producer run must be an existing directory: {run_path}")
    assets = run_path / "assets"
    if not assets.is_dir():
        raise ValueError(f"producer run has no assets directory: {assets}")
    return assets


def _approved_review(raw: dict[str, Any]) -> bool:
    editorial = raw.get("editorial") if isinstance(raw.get("editorial"), dict) else {}
    candidates: list[Any] = [
        editorial.get("human_review_state"), editorial.get("human_review_status"),
        editorial.get("review_status"), raw.get("human_review_state"), raw.get("human_review_status"),
    ]
    for container in (editorial.get("human_review"), raw.get("human_review"), raw.get("review")):
        if isinstance(container, dict):
            candidates.extend((container.get("state"), container.get("status"), container.get("decision")))
    return any(isinstance(value, str) and value.strip().upper() == "APPROVED" for value in candidates)


def validate_movie_broll_eligibility(metadata: AssetMetadataV1, raw: dict[str, Any]) -> None:
    """Accept automatic PASS/legacy KEEP, or REVIEW only after human approval."""

    decision = metadata.editorial.decision.strip().upper()
    if decision in {"PASS", "KEEP"}:
        return
    if decision == "REVIEW" and _approved_review(raw):
        return
    if decision == "REVIEW":
        raise MovieBrollEligibilityError("REVIEW requires separate human APPROVED state")
    raise MovieBrollEligibilityError(f"editorial decision is not eligible: {metadata.editorial.decision!r}")


def normalize_episode_metadata(metadata: AssetMetadataV1, raw: dict[str, Any],
                               identity: EpisodeIdentity) -> NormalizedAsset:
    """Attach Atlas episode identity while retaining all producer evidence verbatim."""

    base = normalize_metadata(metadata, raw, source_key=identity.episode_key)
    provenance = dict(base.producer_provenance)
    provenance["atlas_episode"] = identity.provenance
    return replace(base, producer_provenance=provenance)


def _positive_finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def validate_movie_broll_media(metadata: AssetMetadataV1, json_path: Path,
                                *, ffprobe_runner: FFprobeRunner | None = None) -> None:
    """Decode both producer videos and require usable declared and observed facts."""

    errors: list[str] = []
    for kind in ("horizontal", "vertical"):
        rendition = getattr(metadata.media, kind)
        if not _positive_finite(rendition.duration_seconds):
            errors.append(f"{kind}: declared duration must be positive and finite")
        if not isinstance(rendition.width, int) or rendition.width <= 0 or not isinstance(rendition.height, int) or rendition.height <= 0:
            errors.append(f"{kind}: declared dimensions must be positive")
        try:
            technical = probe_curated_video(json_path.parent / rendition.file, runner=ffprobe_runner)
        except CuratedProbeError as exc:
            errors.append(f"{kind}: media decode failed: {exc}")
            continue
        if not _positive_finite(technical["duration_seconds"]):
            errors.append(f"{kind}: decoded duration must be positive and finite")
        if not isinstance(technical["width"], int) or technical["width"] <= 0 or not isinstance(technical["height"], int) or technical["height"] <= 0:
            errors.append(f"{kind}: decoded dimensions must be positive")
        elif (technical["width"], technical["height"]) != (rendition.width, rendition.height):
            errors.append(f"{kind}: decoded dimensions do not match producer metadata")
    if errors:
        raise MovieBrollTechnicalError("; ".join(errors))


def _existing_state(session_factory: sessionmaker[Session] | None, asset: NormalizedAsset) -> tuple[bool, bool]:
    """Read current natural-key state without opening a transaction or writing."""

    if session_factory is None:
        return False, False
    with session_factory() as session:
        logical = session.scalar(select(LogicalAsset).where(
            LogicalAsset.producer == asset.producer,
            LogicalAsset.source_key == asset.source_key,
            LogicalAsset.producer_asset_id == asset.producer_asset_id,
        ))
        ingest = session.scalar(select(IngestRecord).where(
            IngestRecord.producer == asset.producer,
            IngestRecord.source_key == asset.source_key,
            IngestRecord.producer_asset_id == asset.producer_asset_id,
        ))
    if logical is not None and logical.asset_uid != asset.asset_uid:
        return True, True
    if ingest is not None and ingest.asset_uid not in {None, asset.asset_uid}:
        return True, True
    existing = logical is not None or ingest is not None
    # A half-present logical/ingest pair is recoverable reconciliation.  A
    # wholly absent natural key is simply new.
    return existing, existing and (logical is None or ingest is None)


def preflight_movie_broll(run: str | Path, identity: EpisodeIdentity, *,
                          session_factory: sessionmaker[Session] | None = None,
                          ffprobe_runner: FFprobeRunner | None = None) -> PreflightResult:
    """Run the complete source/readiness preflight without storage or DB writes."""

    directory = assets_directory(run)
    discovery = discover_mbe_packages(directory)
    ids = Counter(item.producer_asset_id for item in discovery.packages if item.producer_asset_id)
    items: list[PreflightItem] = []
    for package in discovery.packages:
        reasons: list[str] = []
        asset: NormalizedAsset | None = None
        if package.status != DiscoveryStatus.READY:
            reasons.append(package.error or package.status.lower())
        if package.producer_asset_id and ids[package.producer_asset_id] > 1:
            reasons.append("duplicate producer asset id")
        if package.metadata is not None:
            try:
                if package.metadata.analysis.producer != "movie_broll_extractor":
                    raise ValueError("analysis.producer must be movie_broll_extractor")
                if package_readiness(package.metadata, package.json_path, interval_seconds=0) != PackageReadiness.VALID:
                    raise ValueError("package is not complete and stable (5/5)")
                validate_trust(package.metadata)
                validate_movie_broll_eligibility(package.metadata, package.metadata.model_dump(mode="python"))
                hashes = validate_referenced_bytes(package.metadata, package.json_path.parent)
                if hashes.readiness != PackageReadiness.VALID:
                    raise ValueError("; ".join(hashes.errors))
                validate_movie_broll_media(package.metadata, package.json_path, ffprobe_runner=ffprobe_runner)
                asset = normalize_episode_metadata(package.metadata, package.metadata.model_dump(mode="python"), identity)
                existing, reconcile = _existing_state(session_factory, asset)
            except Exception as exc:
                reasons.append(str(exc))
                existing = reconcile = False
        else:
            existing = reconcile = False
        items.append(PreflightItem(package, asset, not reasons and asset is not None,
                                   tuple(dict.fromkeys(reasons)), existing, reconcile))
    return PreflightResult(directory, identity, tuple(items))


def ingest_movie_broll_package(json_path: str | Path, identity: EpisodeIdentity,
                               storage_backend: StorageBackend, session_factory: sessionmaker[Session]) -> IngestResult:
    """Use the one established MBE ingest orchestrator with episode identity."""

    return ingest_package(
        json_path, CatalogPlacement("title", title_id=identity.episode_key, title_type="series"),
        storage_backend, session_factory,
        normalizer=lambda metadata, raw: normalize_episode_metadata(metadata, raw, identity),
        metadata_validator=validate_movie_broll_eligibility,
    )


def expected_asset_uid(identity: EpisodeIdentity, producer_asset_id: str) -> str:
    """Small report helper, keeping natural-key construction in one place."""

    return str(atlas_asset_uid("movie_broll_extractor", identity.episode_key, producer_asset_id))
