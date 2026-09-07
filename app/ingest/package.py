"""Read-only package discovery, stability, path safety, and streaming byte checks."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Iterable

from app.contracts.asset_metadata_v1 import AssetMetadataV1


class PackageReadiness(StrEnum):
    NOT_READY = "NOT_READY"
    VALID = "VALID"
    INVALID = "INVALID"


class UnsafePackagePathError(ValueError):
    pass


@dataclass(frozen=True)
class FileSnapshot:
    name: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class HashValidation:
    readiness: PackageReadiness
    errors: tuple[str, ...] = ()


def safe_package_path(source_base_uri: Path, filename: str) -> Path:
    """Resolve a strict package-local filename without permitting path traversal."""

    candidate = Path(filename)
    if not filename or candidate.is_absolute() or candidate.name != filename or ".." in candidate.parts:
        raise UnsafePackagePathError(f"unsafe package-local filename: {filename!r}")
    base = source_base_uri.resolve()
    resolved = (base / candidate).resolve()
    if resolved.parent != base:
        raise UnsafePackagePathError(f"filename resolves outside source_base_uri: {filename!r}")
    return resolved


def referenced_member_paths(metadata: AssetMetadataV1, source_base_uri: Path) -> dict[str, Path]:
    """Return the four producer-hashed members. JSON has no producer self-hash."""

    result: dict[str, Path] = {}
    for kind in ("horizontal", "vertical"):
        rendition = getattr(metadata.media, kind)
        result[kind] = safe_package_path(source_base_uri, rendition.file)
        result[f"{kind}_thumbnail"] = safe_package_path(source_base_uri, rendition.thumbnail.file)
    return result


def capture_snapshot(paths: Iterable[Path]) -> tuple[FileSnapshot, ...] | None:
    """Capture visible regular files; ``None`` means package members are incomplete."""

    snapshots: list[FileSnapshot] = []
    for path in paths:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        if not path.is_file():
            return None
        snapshots.append(FileSnapshot(path.name, stat.st_size, stat.st_mtime_ns))
    return tuple(sorted(snapshots, key=lambda item: item.name))


def readiness_from_snapshots(first: tuple[FileSnapshot, ...] | None,
                             second: tuple[FileSnapshot, ...] | None) -> PackageReadiness:
    """Five visible members (JSON + four references) must be stable before validation."""

    if first is None or second is None or len(first) != 5 or len(second) != 5:
        return PackageReadiness.NOT_READY
    return PackageReadiness.VALID if first == second else PackageReadiness.NOT_READY


def wait_for_stable_snapshot(paths: Iterable[Path], interval_seconds: float = 1.0,
                             sleep: Callable[[float], None] = time.sleep) -> PackageReadiness:
    """Simple manual/integration stability check; tests can inject a no-op sleep."""

    path_list = list(paths)
    first = capture_snapshot(path_list)
    sleep(interval_seconds)
    return readiness_from_snapshots(first, capture_snapshot(path_list))


def package_readiness(metadata: AssetMetadataV1, json_path: Path, interval_seconds: float = 0,
                      sleep: Callable[[float], None] = time.sleep) -> PackageReadiness:
    """Check JSON plus four references; no sixth ready/done sentinel is used."""

    try:
        members = [json_path, *referenced_member_paths(metadata, json_path.parent).values()]
    except UnsafePackagePathError:
        return PackageReadiness.INVALID
    return wait_for_stable_snapshot(members, interval_seconds, sleep)


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_referenced_bytes(metadata: AssetMetadataV1, source_base_uri: Path) -> HashValidation:
    """Hash exactly the four referenced members using streaming reads; never source movie/JSON."""

    errors: list[str] = []
    try:
        paths = referenced_member_paths(metadata, source_base_uri)
    except UnsafePackagePathError as exc:
        return HashValidation(PackageReadiness.INVALID, (str(exc),))
    for kind in ("horizontal", "vertical"):
        rendition = getattr(metadata.media, kind)
        for label, expected, expected_size in (
            (kind, rendition, rendition.size_bytes),
            (f"{kind}_thumbnail", rendition.thumbnail, rendition.thumbnail.size_bytes),
        ):
            path = paths[label]
            if not path.exists() or not path.is_file():
                errors.append(f"{label}: missing or not a regular file")
                continue
            actual_size = path.stat().st_size
            if expected_size is not None and actual_size != expected_size:
                errors.append(f"{label}: size mismatch")
            if sha256_file(path).lower() != expected.sha256.lower():
                errors.append(f"{label}: SHA-256 mismatch")
    return HashValidation(PackageReadiness.INVALID if errors else PackageReadiness.VALID, tuple(errors))
