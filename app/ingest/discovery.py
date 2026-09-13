"""Cheap, read-only discovery of complete MBE producer packages.

Discovery selects package JSONs from one outbox directory.  It deliberately
does not hash media, validate producer trust claims, contact storage, or open a
database session; those are all owned by ``orchestrator.ingest_package``.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import ValidationError

from app.contracts.asset_metadata_v1 import AssetMetadataV1
from app.ingest.package import UnsafePackagePathError, referenced_member_paths, safe_package_path


class DiscoveryStatus(StrEnum):
    """Read-only package selection states."""

    READY = "READY"
    NOT_READY = "NOT_READY"
    INVALID = "INVALID"


@dataclass(frozen=True)
class DiscoveredPackage:
    """One root-level producer JSON and its discovery classification."""

    json_path: Path
    package_basename: str
    status: DiscoveryStatus
    producer_asset_id: str | None = None
    source_movie_id: str | None = None
    metadata: AssetMetadataV1 | None = None
    error: str | None = None


@dataclass(frozen=True)
class DiscoveryResult:
    """A deterministic, entirely source-local outbox observation."""

    outbox: Path
    packages: tuple[DiscoveredPackage, ...]

    @property
    def discovered_json(self) -> int:
        return len(self.packages)

    @property
    def ready(self) -> int:
        return sum(item.status == DiscoveryStatus.READY for item in self.packages)

    @property
    def not_ready(self) -> int:
        return sum(item.status == DiscoveryStatus.NOT_READY for item in self.packages)

    @property
    def invalid(self) -> int:
        return sum(item.status == DiscoveryStatus.INVALID for item in self.packages)


def _read_json_regular_file(path: Path) -> bytes:
    """Read a JSON entry only after rejecting a symlink or non-regular file."""

    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePackagePathError("producer JSON must not be a symlink")
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePackagePathError("producer JSON must be a regular file")
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise UnsafePackagePathError("producer JSON must be a regular file")
        return stream.read()


def _inspect_member(path: Path) -> DiscoveryStatus:
    """Classify a member by presence and type without reading its bytes."""

    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return DiscoveryStatus.NOT_READY
    except OSError:
        # Permission/race errors are retriable source observations, not proof of
        # a malformed producer contract.
        return DiscoveryStatus.NOT_READY
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return DiscoveryStatus.INVALID
    return DiscoveryStatus.READY


def _invalid(path: Path, message: str) -> DiscoveredPackage:
    return DiscoveredPackage(path, path.stem, DiscoveryStatus.INVALID, error=message)


def _discover_json(json_path: Path, outbox: Path) -> DiscoveredPackage:
    try:
        source = _read_json_regular_file(json_path)
    except FileNotFoundError:
        return DiscoveredPackage(json_path, json_path.stem, DiscoveryStatus.NOT_READY,
                                 error="producer JSON disappeared during discovery")
    except (UnsafePackagePathError, OSError) as exc:
        return _invalid(json_path, f"unsafe or unreadable producer JSON: {exc}")
    try:
        raw = json.loads(source.decode("utf-8"))
        metadata = AssetMetadataV1.model_validate(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        return _invalid(json_path, f"malformed asset_metadata_v1 JSON: {exc}")
    if metadata.schema_version != "asset_metadata_v1":
        return _invalid(json_path, f"unsupported schema_version: {metadata.schema_version!r}")

    try:
        safe_package_path(outbox, json_path.name)
        referenced_member_paths(metadata, outbox)
    except UnsafePackagePathError as exc:
        return _invalid(json_path, str(exc))

    # ``referenced_member_paths`` resolves paths for containment validation.
    # Inspect the declared directory entries themselves so a symlink to another
    # package-local file cannot hide behind its resolved regular-file target.
    declared_names = [
        metadata.media.horizontal.file,
        metadata.media.vertical.file,
        metadata.media.horizontal.thumbnail.file,
        metadata.media.vertical.thumbnail.file,
    ]
    if any("\\" in name or Path(name).parts != (name,) for name in declared_names):
        return _invalid(json_path, "package members must use package-local basenames")
    names = [json_path.name, *declared_names]
    if len(names) != 5 or len(set(names)) != 5:
        return _invalid(json_path, "package requires five distinct file names")

    paths = [json_path, *(outbox / name for name in names[1:])]
    member_statuses = [_inspect_member(path) for path in paths]
    if DiscoveryStatus.INVALID in member_statuses:
        return DiscoveredPackage(json_path, json_path.stem, DiscoveryStatus.INVALID,
                                 metadata.asset.id, metadata.asset.source_movie_id, metadata,
                                 "package members must be regular non-symlink files")
    if DiscoveryStatus.NOT_READY in member_statuses:
        return DiscoveredPackage(json_path, json_path.stem, DiscoveryStatus.NOT_READY,
                                 metadata.asset.id, metadata.asset.source_movie_id, metadata,
                                 "one or more package members are not present")
    return DiscoveredPackage(json_path, json_path.stem, DiscoveryStatus.READY,
                             metadata.asset.id, metadata.asset.source_movie_id, metadata)


def discover_mbe_packages(outbox: str | Path) -> DiscoveryResult:
    """Discover root-level ``*.json`` entries in lexical filename order.

    ``review/``, hidden directories, and all nested directories are ignored by
    construction: this function only enumerates direct children of ``outbox``.
    """

    source_dir = Path(outbox).absolute()
    if not source_dir.is_dir():
        raise ValueError(f"MBE outbox must be an existing directory: {source_dir}")
    json_paths = sorted(
        (entry for entry in source_dir.iterdir()
         if entry.name.endswith(".json") and not entry.name.startswith(".")),
        key=lambda entry: entry.name,
    )
    return DiscoveryResult(source_dir, tuple(_discover_json(path, source_dir) for path in json_paths))
