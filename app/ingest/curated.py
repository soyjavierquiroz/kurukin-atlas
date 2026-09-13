"""Curated file ingest: explicit metadata and technical ffprobe only, never AI."""

from __future__ import annotations

import json
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence

from sqlalchemy.orm import Session, sessionmaker

from app.contracts.curated_asset_v1 import CuratedAssetV1, CuratedRenditionV1
from app.ingest.catalog import CatalogIngestRequest, CatalogPlacement, catalog_validated_asset
from app.ingest.fingerprint import observed_package_fingerprint
from app.ingest.normalize import NormalizedAsset, NormalizedRendition, atlas_asset_uid
from app.ingest.orchestrator import IngestResult, IngestStatus
from app.ingest.verification import verify_cataloged_asset
from app.storage.contracts import VerifiedStoredPackage
from app.storage.errors import StoragePathError
from app.storage.rclone_drive import RcloneDrivePublisher


class CuratedSourceValidationError(ValueError):
    """The submitted local source is not a safe curated video input."""


class CuratedProbeError(RuntimeError):
    """ffprobe could not establish the video's technical evidence."""


class CuratedProbeTimeoutError(CuratedProbeError):
    """ffprobe exceeded its caller-provided bounded execution time."""


class FFprobeRunner(Protocol):
    """Narrow injectable subprocess seam; implementations receive an argv array."""

    def run(self, args: Sequence[str], *, timeout_seconds: float) -> bytes: ...


class SubprocessFFprobeRunner:
    """Run ffprobe without a shell and return only its JSON stdout."""

    def run(self, args: Sequence[str], *, timeout_seconds: float) -> bytes:
        try:
            completed = subprocess.run(
                list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False, timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise CuratedProbeError("ffprobe executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise CuratedProbeTimeoutError("ffprobe timed out") from exc
        except OSError as exc:
            raise CuratedProbeError("ffprobe could not start") from exc
        if completed.returncode != 0:
            raise CuratedProbeError(f"ffprobe failed (exit {completed.returncode})")
        return completed.stdout


@dataclass(frozen=True)
class CuratedImportSpec:
    """Caller-supplied editorial/import evidence for one pre-cut local video."""

    source_path: Path
    collection_id: str
    producer_asset_id: str | None = None
    rendition_kind: Literal["vertical"] = "vertical"
    metadata_source: Literal["editorial", "filename", "collection_defaults", "none"] = "none"
    visual_summary: str | None = None
    keywords: list[str] = field(default_factory=list)
    search_terms: list[str] = field(default_factory=list)
    negative_use_cases: list[str] = field(default_factory=list)
    standalone_meaning: str | None = None
    collection_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.collection_id, str) or not self.collection_id.strip() or self.collection_id != self.collection_id.strip():
            raise CuratedSourceValidationError("collection_id must be a non-empty normalized string")
        if self.producer_asset_id is not None and (
            not isinstance(self.producer_asset_id, str) or not self.producer_asset_id.strip()
            or self.producer_asset_id != self.producer_asset_id.strip()
        ):
            raise CuratedSourceValidationError("producer_asset_id must be a non-empty normalized string when supplied")
        semantic_claims = any((self.visual_summary, self.keywords, self.search_terms,
                               self.negative_use_cases, self.standalone_meaning))
        if semantic_claims and self.metadata_source == "none":
            raise CuratedSourceValidationError("curated semantic claims require a metadata_source")


@dataclass(frozen=True)
class CuratedSourceSnapshot:
    size_bytes: int
    mtime_ns: int


class CuratedStorageBackend(Protocol):
    """Storage handoff for the tiny manifest plus source video; no local video copy."""

    def store_curated_asset(self, metadata: CuratedAssetV1, normalized_asset: NormalizedAsset,
                            source_path: Path, manifest_path: Path,
                            observed_package_fingerprint: str) -> VerifiedStoredPackage: ...


def curated_source_snapshot(path: Path) -> CuratedSourceSnapshot:
    """Check the source itself with lstat, never following a substituted symlink."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise CuratedSourceValidationError("cannot inspect curated source") from exc
    if stat.S_ISLNK(info.st_mode):
        raise CuratedSourceValidationError("curated source must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise CuratedSourceValidationError("curated source must be a regular file")
    if info.st_size <= 0:
        raise CuratedSourceValidationError("curated source must be non-empty")
    return CuratedSourceSnapshot(info.st_size, info.st_mtime_ns)


def streaming_sha256(path: Path) -> tuple[str, int]:
    """Reuse Drive custody's safe regular-file streaming hash for source evidence."""
    try:
        return RcloneDrivePublisher.hash_file(path)
    except StoragePathError as exc:
        raise CuratedSourceValidationError("cannot safely hash curated source") from exc


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _fps(value: Any) -> float | None:
    if not isinstance(value, str) or not value or value == "0/0":
        return None
    try:
        numerator, denominator = value.split("/", 1)
        result = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return None
    return result if result > 0 else None


def probe_curated_video(path: Path, *, runner: FFprobeRunner | None = None,
                        timeout_seconds: float = 30.0) -> dict[str, Any]:
    """Return technical-only video facts from ffprobe; never inspect semantics."""
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise CuratedProbeError("ffprobe timeout must be positive")
    argv = (
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,codec_type,width,height,avg_frame_rate,r_frame_rate,duration:format=duration,format_name",
        "-of", "json", str(path),
    )
    raw = (runner or SubprocessFFprobeRunner()).run(argv, timeout_seconds=float(timeout_seconds))
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CuratedProbeError("ffprobe returned malformed JSON") from exc
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        raise CuratedProbeError("ffprobe returned no video stream list")
    stream = next((item for item in streams if isinstance(item, dict) and item.get("codec_type", "video") == "video"), None)
    if stream is None:
        raise CuratedProbeError("ffprobe found no video stream")
    width = stream.get("width") if isinstance(stream.get("width"), int) and stream["width"] > 0 else None
    height = stream.get("height") if isinstance(stream.get("height"), int) and stream["height"] > 0 else None
    duration = _number(stream.get("duration"))
    if duration is None and isinstance(payload.get("format"), dict):
        duration = _number(payload["format"].get("duration"))
    fps = _fps(stream.get("avg_frame_rate")) or _fps(stream.get("r_frame_rate"))
    orientation = "vertical" if width is not None and height is not None and width < height else (
        "horizontal" if width is not None and height is not None and width > height else None
    )
    format_name = payload.get("format", {}).get("format_name", "") if isinstance(payload.get("format"), dict) else ""
    mime_type = "video/mp4" if "mp4" in str(format_name).split(",") else None
    return {
        "mime_type": mime_type, "duration_seconds": duration, "width": width, "height": height,
        "fps": fps, "orientation": orientation,
        "aspect_ratio": f"{width}:{height}" if width is not None and height is not None else None,
        # codec_name is intentionally not mapped into semantic contract fields.
    }


def filename_terms(path: str | Path, *, remove_prefix: str | None = None) -> str:
    """Conservative deterministic filename text helper; it performs no NLP."""
    stem = Path(path).stem
    if remove_prefix and stem.startswith(remove_prefix):
        stem = stem[len(remove_prefix):].lstrip("_-")
    return " ".join(stem.replace("_", " ").replace("-", " ").split())


def canonical_curated_manifest(metadata: CuratedAssetV1) -> bytes:
    """The exact UTF-8 custody bytes for Atlas-generated curated metadata."""
    return json.dumps(metadata.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def normalize_curated_asset(metadata: CuratedAssetV1, raw_metadata: dict[str, Any] | None = None) -> NormalizedAsset:
    """Map editorial or filename-derived input into the common Atlas representation."""

    source_key = f"collection:{metadata.collection_id}"
    renditions = {
        item.kind: NormalizedRendition(
            kind=item.kind, filename=item.filename, sha256=item.sha256, size_bytes=item.size_bytes,
            mime_type=item.mime_type, duration_seconds=item.duration_seconds, width=item.width,
            height=item.height, fps=item.fps, orientation=item.orientation,
            aspect_ratio=item.aspect_ratio, technical_validated=item.technical_validated,
            semantic_validated=item.semantic_validated, thumbnail=item.thumbnail,
        )
        for item in metadata.renditions
    }
    raw = metadata.model_dump(mode="python") if raw_metadata is None else raw_metadata
    provenance = {
        "metadata_source": metadata.metadata_source,
        "collection_id": metadata.collection_id,
        "collection_metadata": metadata.collection_metadata,
        "semantic_inference": "none",
    }
    return NormalizedAsset(
        asset_uid=atlas_asset_uid(metadata.producer, source_key, metadata.producer_asset_id),
        producer=metadata.producer, producer_asset_id=metadata.producer_asset_id,
        source_key=source_key, source_kind="collection", source_movie_sha256=None,
        slug=None, editorial_decision=metadata.editorial_decision, editorial_status=metadata.editorial_status,
        reusable_broll=None, standalone_meaning=metadata.standalone_meaning,
        action_or_moment_complete=None, keywords=metadata.keywords, search_terms=metadata.search_terms,
        negative_use_cases=metadata.negative_use_cases, visual_summary=metadata.visual_summary,
        subjects=[], objects=[], actions=[], visible_emotions=[], interactions=[], setting=None,
        people_json={}, relationships_json=[], rendition_overrides={}, narrative_json=None,
        source_timeline=None, producer_provenance=provenance, renditions=renditions,
        raw_producer_metadata=raw,
    )


def ingest_curated_file(spec: CuratedImportSpec, storage_backend: CuratedStorageBackend,
                        session_factory: sessionmaker[Session], *, ffprobe_runner: FFprobeRunner | None = None,
                        ffprobe_timeout_seconds: float = 30.0) -> IngestResult:
    """Ingest one stable pre-cut curated MP4 through Drive, catalog, then fresh verification.

    This is intentionally independent from ``ingest_package``: that function is
    the frozen producer MBE five-file boundary.  Curated source bytes stay in
    place; the only temporary local material is ``atlas-curated.json``.
    """
    source_path = Path(spec.source_path).absolute()
    if source_path.suffix.lower() != ".mp4":
        raise CuratedSourceValidationError("curated source must be an MP4")
    try:
        before = curated_source_snapshot(source_path)
    except FileNotFoundError:
        return IngestResult(IngestStatus.NOT_READY)
    sha256, size_bytes = streaming_sha256(source_path)
    technical = probe_curated_video(source_path, runner=ffprobe_runner,
                                    timeout_seconds=ffprobe_timeout_seconds)
    try:
        after = curated_source_snapshot(source_path)
    except FileNotFoundError:
        return IngestResult(IngestStatus.NOT_READY)
    if before != after or size_bytes != before.size_bytes:
        return IngestResult(IngestStatus.NOT_READY)
    if technical["orientation"] not in {None, spec.rendition_kind}:
        raise CuratedSourceValidationError("curated source orientation does not match requested rendition kind")

    producer_asset_id = spec.producer_asset_id or source_path.stem
    metadata = CuratedAssetV1(
        schema_version="curated_asset_v1", collection_id=spec.collection_id,
        producer_asset_id=producer_asset_id,
        renditions=[CuratedRenditionV1(
            kind=spec.rendition_kind, filename=source_path.name, sha256=sha256,
            size_bytes=size_bytes, technical_validated=True, semantic_validated=False,
            thumbnail={}, **technical,
        )],
        visual_summary=spec.visual_summary, keywords=spec.keywords,
        search_terms=spec.search_terms, negative_use_cases=spec.negative_use_cases,
        standalone_meaning=spec.standalone_meaning, metadata_source=spec.metadata_source,
        collection_metadata=spec.collection_metadata,
    )
    raw_metadata = metadata.model_dump(mode="json")
    asset = normalize_curated_asset(metadata, raw_metadata)
    fingerprint = observed_package_fingerprint(asset)
    manifest_bytes = canonical_curated_manifest(metadata)
    with tempfile.TemporaryDirectory(prefix="atlas-curated-") as temporary_directory:
        manifest_path = Path(temporary_directory) / "atlas-curated.json"
        # The directory was securely created by tempfile; exclusive creation
        # prevents an accidental replacement before Drive reads the manifest.
        with manifest_path.open("xb") as stream:
            stream.write(manifest_bytes)
        stored = storage_backend.store_curated_asset(metadata, asset, source_path, manifest_path, fingerprint)
    if stored.package_fingerprint != fingerprint:
        from app.storage.errors import StorageIntegrityError
        raise StorageIntegrityError("storage package fingerprint does not match requested fingerprint")
    request = CatalogIngestRequest(
        normalized_asset=asset, placement=CatalogPlacement("general"),
        verified_storage_manifest=stored.manifest,
        source_base_uri=source_path.parent.resolve().as_uri(), package_basename=source_path.stem,
        destination_verified_at=stored.destination_verified_at,
        observed_package_fingerprint=fingerprint,
    )
    with session_factory.begin() as session:
        catalog_result = catalog_validated_asset(session, request)
    with session_factory() as verification_session:
        state = verify_cataloged_asset(verification_session, request)
    return IngestResult(
        status=IngestStatus.CATALOGED, asset_uid=asset.asset_uid,
        package_fingerprint=fingerprint, storage_created=stored.created,
        logical_asset_created=catalog_result.logical_asset_created,
        package_changed=catalog_result.package_changed, ingest_state=state,
        catalog_verified=True,
    )
