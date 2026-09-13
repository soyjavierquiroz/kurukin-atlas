"""Immutable rclone/Google Drive custody for Atlas package members.

Publication is source -> unique remote staging -> verified staging -> server-side
directory move -> verified final.  The lower-level ``RemoteMember`` and remote
publication helpers deliberately do not require five members, so a future
curated importer can reuse them without changing Drive publication semantics.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Sequence
from urllib.parse import quote

from app.contracts.asset_metadata_v1 import AssetMetadataV1
from app.contracts.curated_asset_v1 import CuratedAssetV1
from app.ingest.fingerprint import observed_package_fingerprint as fingerprint_for
from app.ingest.normalize import NormalizedAsset, atlas_asset_uid
from app.storage.contracts import VerifiedRenditionLocation, VerifiedStorageManifest, VerifiedStoredPackage
from app.storage.errors import RcloneTransportError, StorageInputError, StorageIntegrityError, StoragePathError
from app.storage.rclone import RcloneCommandResult, RcloneCommandRunner, SubprocessRcloneCommandRunner

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def encode_path_component(value: str) -> str:
    """Encode a logical identity as exactly one reversible Drive path component."""

    if not isinstance(value, str) or not value or value.strip() != value:
        raise StoragePathError("storage path component must be a non-empty normalized string")
    if value in {".", ".."} or "/" in value or "\\" in value or ".." in PurePosixPath(value).parts:
        raise StoragePathError("storage path component cannot contain traversal")
    return quote(value, safe="-_.~")


@dataclass(frozen=True)
class RemoteMember:
    local_path: Path
    remote_name: str
    sha256: str
    size_bytes: int


def _join(*parts: str) -> str:
    return "/".join(part.strip("/") for part in parts if part != "")


def _validate_remote(remote: str) -> str:
    if not isinstance(remote, str) or not remote.strip() or remote != remote.strip() or not remote.endswith(":"):
        raise StorageInputError("rclone remote must be a non-empty rclone remote ending in ':'")
    if remote == ":" or "/" in remote or "\\" in remote:
        raise StorageInputError("rclone remote must be a remote name ending in ':'")
    return remote


def _validate_root(root: str) -> str:
    if not isinstance(root, str) or not root.strip() or root != root.strip():
        raise StorageInputError("rclone root must be a non-empty relative path")
    path = PurePosixPath(root)
    if root in {".", ".."} or path.is_absolute() or "\\" in root or any(part in {"", ".", ".."} for part in path.parts):
        raise StorageInputError("rclone root must be relative and contain no traversal")
    return str(path)


@contextmanager
def _writer_lock(path: Path) -> Iterator[None]:
    """P0 serialization for cooperating Drive writers on this Atlas host only."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise StoragePathError("cannot safely open Drive writer lock") from exc
    try:
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise StoragePathError("Drive writer lock must be a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise StoragePathError("cannot safely lock Drive writer lock") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class PublishedRemoteDirectory:
    """A final Drive directory already checked against every expected member."""

    final_dir: str
    verified_at: datetime
    created: bool


class RcloneDrivePublisher:
    """Generic immutable Drive publication for explicit, pre-validated members.

    This class deliberately owns no producer package interpretation.  Callers
    provide the normalized identity, fingerprint, and the complete member set;
    it supplies the one shared staging, verification, promotion, replay, and
    URI protocol used by both MBE and curated ingestion.
    """

    def __init__(self, *, binary: str, remote: str, root: str,
                 runner: RcloneCommandRunner | None = None, timeout_seconds: float = 120.0,
                 lock_path: str | Path | None = None) -> None:
        if not isinstance(binary, str) or not binary.strip() or binary != binary.strip():
            raise StorageInputError("rclone binary must be non-empty")
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise StorageInputError("rclone timeout must be positive")
        self.binary = binary
        self.remote = _validate_remote(remote)
        self.root = _validate_root(root)
        self.timeout_seconds = float(timeout_seconds)
        self.runner = runner or SubprocessRcloneCommandRunner()
        # The file is not opened (or its parent created) until publish().
        self.lock_path = (Path(lock_path) if lock_path is not None else
                          Path("/opt/apps/kurukin-atlas/data/locks/rclone-drive.lock"))

    def publish(self, normalized_asset: NormalizedAsset, observed_package_fingerprint: str,
                members: Sequence[RemoteMember]) -> PublishedRemoteDirectory:
        """Publish exactly ``members`` or verify an immutable exact replay."""
        self._validate_publish_inputs(normalized_asset, observed_package_fingerprint, members)
        final_dir = self._final_dir(normalized_asset, observed_package_fingerprint)
        with _writer_lock(self.lock_path):
            self._mkdir_final_parent(normalized_asset)
            if self._directory_exists(final_dir):
                self._verify_directory(final_dir, members)
                return PublishedRemoteDirectory(final_dir, datetime.now(timezone.utc), created=False)

            stage_dir = _join(self.root, "_staging", str(uuid.uuid4()))
            self._mkdir_staging(stage_dir)
            promoted = False
            try:
                for member in members:
                    self._run("copyto", str(member.local_path), self._remote_path(_join(stage_dir, member.remote_name)))
                self._verify_directory(stage_dir, members)
                if self._directory_exists(final_dir):
                    raise StorageIntegrityError("final package appeared during staging")
                self._run("moveto", self._remote_path(stage_dir), self._remote_path(final_dir))
                promoted = True
                self._verify_directory(final_dir, members)
            except BaseException:
                if not promoted:
                    self._purge_staging(stage_dir)
                raise
            return PublishedRemoteDirectory(final_dir, datetime.now(timezone.utc), created=True)

    def _validate_publish_inputs(self, asset: NormalizedAsset, fingerprint: str,
                                 members: Sequence[RemoteMember]) -> None:
        if not isinstance(fingerprint, str) or not _SHA256.fullmatch(fingerprint):
            raise StorageInputError("observed_package_fingerprint must be a lowercase full SHA-256")
        if fingerprint != fingerprint_for(asset):
            raise StorageInputError("observed_package_fingerprint does not match normalized_asset")
        if not members:
            raise StorageInputError("Drive publication requires at least one member")
        names: set[str] = set()
        for member in members:
            if not isinstance(member, RemoteMember):
                raise StorageInputError("Drive publication members must be RemoteMember records")
            name = member.remote_name
            candidate = Path(name)
            if (not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name
                    or candidate.is_absolute() or candidate.name != name or candidate.parts != (name,)):
                raise StoragePathError(f"unsafe remote member filename: {name!r}")
            if name in names:
                raise StorageInputError("Drive publication members must have distinct filenames")
            names.add(name)
            if not _SHA256.fullmatch(member.sha256) or member.size_bytes < 0:
                raise StorageInputError("Drive publication member hash or size is invalid")
            try:
                info = member.local_path.lstat()
            except OSError as exc:
                raise StoragePathError(f"cannot inspect publication member {name!r}") from exc
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise StoragePathError(f"publication member {name!r} must be a regular non-symlink file")
            if info.st_size != member.size_bytes:
                raise StorageIntegrityError(f"publication member {name!r}: size changed before publication")

    @staticmethod
    def safe_regular_file(source_dir: Path, name: str) -> Path:
        """Return a package-local regular non-symlink source file."""
        candidate = Path(name)
        if (not isinstance(name, str) or not name or name in {".", ".."} or "/" in name or "\\" in name
                or candidate.is_absolute() or candidate.name != name or candidate.parts != (name,)):
            raise StoragePathError(f"unsafe package-local filename: {name!r}")
        path = source_dir / candidate
        try:
            info = path.lstat()
        except OSError as exc:
            raise StoragePathError(f"cannot inspect source {name!r}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise StoragePathError(f"source {name!r} must not be a symlink")
        if not stat.S_ISREG(info.st_mode):
            raise StorageInputError(f"source {name!r} must be a regular file")
        return path

    @staticmethod
    def hash_file(path: Path) -> tuple[str, int]:
        """Stream a regular non-symlink file through a safely opened descriptor."""

        flags = os.O_RDONLY | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise StoragePathError(f"cannot safely open source {str(path)!r}") from exc
        digest = hashlib.sha256()
        size = 0
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise StoragePathError(f"source {str(path)!r} must be a regular file")
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            raise StoragePathError(f"cannot read source {str(path)!r}") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        return digest.hexdigest(), size

    def _mbe_members(self, metadata: AssetMetadataV1, asset: NormalizedAsset, json_path: Path,
                     fingerprint: str) -> tuple[RemoteMember, ...]:
        if not isinstance(fingerprint, str) or not _SHA256.fullmatch(fingerprint):
            raise StorageInputError("observed_package_fingerprint must be a lowercase full SHA-256")
        if fingerprint != fingerprint_for(asset):
            raise StorageInputError("observed_package_fingerprint does not match normalized_asset")
        identity = (metadata.analysis.producer, metadata.asset.source_movie_id, metadata.asset.id)
        if identity != (asset.producer, asset.source_key, asset.producer_asset_id) or asset.asset_uid != atlas_asset_uid(*identity):
            raise StorageInputError("metadata identity does not match normalized_asset")
        if set(asset.renditions) != {"horizontal", "vertical"}:
            raise StorageInputError("Drive MBE custody requires horizontal and vertical renditions")

        source_dir = json_path.parent
        sources: list[tuple[Path, str, str, int | None]] = []
        for kind in ("horizontal", "vertical"):
            producer = getattr(metadata.media, kind)
            normalized = asset.renditions[kind]
            if (producer.file, producer.sha256, producer.size_bytes, producer.thumbnail.model_dump(mode="python")) != (
                normalized.filename, normalized.sha256, normalized.size_bytes, normalized.thumbnail
            ):
                raise StorageInputError(f"metadata {kind} members do not match normalized_asset")
            sources.extend(((self.safe_regular_file(source_dir, producer.file), producer.file, producer.sha256, producer.size_bytes),
                            (self.safe_regular_file(source_dir, producer.thumbnail.file), producer.thumbnail.file,
                             producer.thumbnail.sha256, producer.thumbnail.size_bytes)))
        source_json = self.safe_regular_file(source_dir, json_path.name)
        self._bind_producer_json(source_json, asset)
        sources.append((source_json, json_path.name, "", None))
        names = [name for _, name, _, _ in sources]
        if len(sources) != 5 or len(set(names)) != 5:
            raise StorageInputError("package requires five distinct source files and filenames")

        members: list[RemoteMember] = []
        for source, name, expected_hash, expected_size in sources:
            actual_hash, actual_size = self.hash_file(source)
            if expected_hash and actual_hash != expected_hash.lower():
                raise StorageIntegrityError(f"source {name!r}: SHA-256 mismatch")
            if expected_size is not None and actual_size != expected_size:
                raise StorageIntegrityError(f"source {name!r}: size mismatch")
            members.append(RemoteMember(source, name, actual_hash, actual_size))
        return tuple(members)

    @staticmethod
    def _bind_producer_json(source_json: Path, asset: NormalizedAsset) -> None:
        """Require the uploaded producer JSON to be the normalized asset's source."""

        try:
            raw_bytes = source_json.read_bytes()
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StorageInputError("source producer JSON must be valid UTF-8 JSON") from exc
        if not isinstance(raw, dict):
            raise StorageInputError("source producer JSON must be an object")
        try:
            AssetMetadataV1.model_validate(raw)
        except Exception as exc:
            raise StorageInputError("source producer JSON does not validate as AssetMetadataV1") from exc
        if raw != asset.raw_producer_metadata:
            raise StorageInputError("source producer JSON does not match normalized_asset raw metadata")

    def _final_dir(self, asset: NormalizedAsset, fingerprint: str) -> str:
        return _join(self._final_parent(asset), f"{encode_path_component(asset.producer_asset_id)}--{fingerprint}")

    def _final_parent(self, asset: NormalizedAsset) -> str:
        return _join(self.root, "assets", encode_path_component(asset.producer), encode_path_component(asset.source_key))

    def _remote_path(self, relative_path: str) -> str:
        return f"{self.remote}{relative_path}"

    def _run(self, command: str, *arguments: str) -> RcloneCommandResult:
        if command not in {"mkdir", "lsjson", "copyto", "moveto", "purge", "cat"}:
            raise AssertionError("unapproved rclone command")
        suffix: Sequence[str] = ("--hash",) if command == "lsjson" else ()
        return self.runner.run((self.binary, command, *arguments, *suffix), timeout_seconds=self.timeout_seconds)

    def _mkdir_final_parent(self, asset: NormalizedAsset) -> None:
        """Create exactly the derived producer/source organizational parent."""

        self._run("mkdir", self._remote_path(self._final_parent(asset)))

    def _mkdir_staging(self, stage_dir: str) -> None:
        """Create exactly one internally-generated UUID staging directory."""

        self._require_uuid_staging_dir(stage_dir)
        self._run("mkdir", self._remote_path(stage_dir))

    def _lsjson(self, directory: str) -> list[dict[str, Any]]:
        result = self._run("lsjson", self._remote_path(directory))
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RcloneTransportError("rclone lsjson returned malformed JSON") from exc
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise RcloneTransportError("rclone lsjson returned malformed JSON")
        return payload

    def _directory_exists(self, directory: str) -> bool:
        parent, name = directory.rsplit("/", 1)
        matches = [item for item in self._lsjson(parent) if item.get("Name") == name]
        if not matches:
            return False
        if len(matches) != 1 or item_is_dir(matches[0]) is not True:
            raise StorageIntegrityError("final package path exists but is not a directory")
        return True

    def _verify_directory(self, directory: str, members: Sequence[RemoteMember]) -> None:
        entries = self._lsjson(directory)
        expected = {member.remote_name: member for member in members}
        names: list[str] = []
        for entry in entries:
            name = entry.get("Name")
            if not isinstance(name, str) or not name or "/" in name or "\\" in name:
                raise RcloneTransportError("rclone lsjson returned malformed JSON")
            names.append(name)
            if item_is_dir(entry) is not False:
                raise StorageIntegrityError("remote package contains a non-file member")
        if set(names) != set(expected) or len(names) != len(expected):
            raise StorageIntegrityError("remote package does not contain exactly the expected members")
        by_name = {entry["Name"]: entry for entry in entries}
        for name, member in expected.items():
            entry = by_name[name]
            if entry.get("Size") != member.size_bytes:
                raise StorageIntegrityError(f"remote {name!r}: size mismatch")
            digest = self._sha256_from_entry(entry)
            if digest is None:
                digest = self._cat_sha256(_join(directory, name))
            if digest != member.sha256:
                raise StorageIntegrityError(f"remote {name!r}: SHA-256 mismatch")

    @staticmethod
    def _sha256_from_entry(entry: dict[str, Any]) -> str | None:
        hashes = entry.get("Hashes")
        if hashes is None:
            return None
        if not isinstance(hashes, dict):
            raise RcloneTransportError("rclone lsjson returned malformed JSON")
        for key, value in hashes.items():
            normalized = str(key).lower().replace("-", "")
            if normalized == "sha256":
                if not isinstance(value, str) or not _SHA256.fullmatch(value.lower()):
                    raise RcloneTransportError("rclone lsjson returned malformed JSON")
                return value.lower()
        return None

    def _cat_sha256(self, remote_file: str) -> str:
        digest = hashlib.sha256()
        # This fallback is used only when Drive's lsjson response lacks SHA-256;
        # it streams the object, so media bytes are never retained in memory.
        for chunk in self.runner.stream((self.binary, "cat", self._remote_path(remote_file)), timeout_seconds=self.timeout_seconds):
            digest.update(chunk)
        return digest.hexdigest()

    def _purge_staging(self, stage_dir: str) -> None:
        self._require_uuid_staging_dir(stage_dir)
        try:
            self._run("purge", self._remote_path(stage_dir))
        except RcloneTransportError:
            # The caller's original exception is authoritative; this is a
            # best-effort removal of only this operation's UUID staging path.
            pass

    def _require_uuid_staging_dir(self, stage_dir: str) -> None:
        prefix = _join(self.root, "_staging") + "/"
        suffix = stage_dir.removeprefix(prefix)
        try:
            parsed = uuid.UUID(suffix)
        except (AttributeError, ValueError) as exc:
            raise StoragePathError("staging path must be this operation's UUID directory") from exc
        if stage_dir != prefix + str(parsed):
            raise StoragePathError("staging path must be this operation's UUID directory")

    def _uri(self, relative_path: str) -> str:
        # Percent signs in physical Drive names must themselves be encoded so
        # one ordinary URI decode recovers the exact rclone-relative path.
        return f"rclone://{self.remote[:-1]}/{quote(relative_path, safe='/-_.~')}"


class RcloneDriveStorageBackend:
    """MBE five-member adapter over the generic immutable Drive publisher."""

    def __init__(self, *, binary: str, remote: str, root: str,
                 runner: RcloneCommandRunner | None = None, timeout_seconds: float = 120.0,
                 lock_path: str | Path | None = None) -> None:
        self.publisher = RcloneDrivePublisher(
            binary=binary, remote=remote, root=root, runner=runner,
            timeout_seconds=timeout_seconds, lock_path=lock_path,
        )

    def store_package(self, metadata: AssetMetadataV1, normalized_asset: NormalizedAsset,
                      json_path: Path, observed_package_fingerprint: str) -> VerifiedStoredPackage:
        members = self.publisher._mbe_members(metadata, normalized_asset, Path(json_path), observed_package_fingerprint)
        published = self.publisher.publish(normalized_asset, observed_package_fingerprint, members)
        def location(kind: str) -> VerifiedRenditionLocation:
            rendition = getattr(metadata.media, kind)
            return VerifiedRenditionLocation(self._uri(_join(published.final_dir, rendition.file)),
                                             self._uri(_join(published.final_dir, rendition.thumbnail.file)))
        return VerifiedStoredPackage(
            manifest=VerifiedStorageManifest(horizontal=location("horizontal"), vertical=location("vertical")),
            destination_verified_at=published.verified_at, destination_base_uri=self._uri(published.final_dir),
            metadata_uri=self._uri(_join(published.final_dir, Path(json_path).name)),
            package_fingerprint=observed_package_fingerprint, created=published.created,
        )

    # Compatibility inspection seams retained for the existing Drive tests.
    def _final_dir(self, asset: NormalizedAsset, fingerprint: str) -> str:
        return self.publisher._final_dir(asset, fingerprint)

    def _uri(self, relative_path: str) -> str:
        return self.publisher._uri(relative_path)

    def _purge_staging(self, stage_dir: str) -> None:
        self.publisher._purge_staging(stage_dir)


class RcloneDriveCuratedStorageBackend:
    """Curated two-member adapter over the same immutable Drive publisher."""

    manifest_name = "atlas-curated.json"

    def __init__(self, *, binary: str, remote: str, root: str,
                 runner: RcloneCommandRunner | None = None, timeout_seconds: float = 120.0,
                 lock_path: str | Path | None = None,
                 publisher: RcloneDrivePublisher | None = None) -> None:
        self.publisher = publisher or RcloneDrivePublisher(
            binary=binary, remote=remote, root=root, runner=runner,
            timeout_seconds=timeout_seconds, lock_path=lock_path,
        )

    def store_curated_asset(self, metadata: CuratedAssetV1, normalized_asset: NormalizedAsset,
                            source_path: Path, manifest_path: Path,
                            observed_package_fingerprint: str) -> VerifiedStoredPackage:
        if set(normalized_asset.renditions) != {"vertical"}:
            raise StorageInputError("initial curated Drive custody requires exactly one vertical rendition")
        rendition = normalized_asset.renditions["vertical"]
        if source_path.name != rendition.filename:
            raise StorageInputError("curated source filename does not match normalized vertical rendition")
        if manifest_path.name != self.manifest_name:
            raise StorageInputError("curated manifest must be named atlas-curated.json")
        expected_raw = metadata.model_dump(mode="json")
        if normalized_asset.raw_producer_metadata != expected_raw:
            raise StorageInputError("curated metadata does not match normalized raw metadata")
        expected_manifest = json.dumps(expected_raw, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")
        expected_manifest_hash = hashlib.sha256(expected_manifest).hexdigest()
        try:
            manifest_hash, manifest_size = RcloneDrivePublisher.hash_file(Path(manifest_path))
            if manifest_hash != expected_manifest_hash or manifest_size != len(expected_manifest):
                raise StorageInputError("curated manifest is not the canonical Atlas metadata bytes")
        except StoragePathError as exc:
            raise StoragePathError("cannot read curated manifest") from exc
        try:
            source_hash, source_size = RcloneDrivePublisher.hash_file(Path(source_path))
        except StoragePathError as exc:
            raise StorageIntegrityError("curated source cannot be safely rebound before publication") from exc
        if source_hash != rendition.sha256 or source_size != rendition.size_bytes:
            raise StorageIntegrityError("curated source bytes do not match normalized vertical rendition")
        video = RemoteMember(Path(source_path), rendition.filename, source_hash, source_size)
        manifest = RemoteMember(Path(manifest_path), self.manifest_name, manifest_hash, manifest_size)
        published = self.publisher.publish(normalized_asset, observed_package_fingerprint, (video, manifest))
        return VerifiedStoredPackage(
            manifest=VerifiedStorageManifest({"vertical": VerifiedRenditionLocation(
                self.publisher._uri(_join(published.final_dir, rendition.filename)), None,
            )}),
            destination_verified_at=published.verified_at,
            destination_base_uri=self.publisher._uri(published.final_dir),
            metadata_uri=self.publisher._uri(_join(published.final_dir, self.manifest_name)),
            package_fingerprint=observed_package_fingerprint,
            created=published.created,
        )


def item_is_dir(entry: dict[str, Any]) -> bool | None:
    value = entry.get("IsDir")
    return value if isinstance(value, bool) else None
