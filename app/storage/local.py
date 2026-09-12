"""Atlas-owned local custody: copy, verify, then atomically publish.

Directory descriptors and O_NOFOLLOW reject destination symlinks, including
symlinks to locations within the root. An advisory root lock serializes Atlas
writers (including separate processes). The root must remain Atlas-owned;
uncooperative processes must not rename directories or modify published bytes.
No source mutation or catalog operations are performed here.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import stat
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.contracts.asset_metadata_v1 import AssetMetadataV1
from app.ingest.fingerprint import observed_package_fingerprint as fingerprint_for
from app.ingest.normalize import NormalizedAsset, atlas_asset_uid
from app.ingest.package import UnsafePackagePathError, safe_package_path, sha256_file
from app.storage.contracts import VerifiedRenditionLocation, VerifiedStorageManifest, VerifiedStoredPackage
from app.storage.errors import StorageError, StorageInputError, StorageIntegrityError, StoragePathError


@contextmanager
def _directory(parent_fd: int | None, name: str, *, create: bool = False):
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise StoragePathError(f"cannot open safe destination directory {name!r}: {exc}") from exc
    try:
        yield fd
    finally:
        os.close(fd)


def _verify(directory_fd: int, members: dict[str, tuple[Path, str, int | None]]) -> None:
    if set(os.listdir(directory_fd)) != set(members):
        raise StorageIntegrityError("destination must contain exactly the five package files")
    for name, (_, expected_hash, expected_size) in members.items():
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise StorageIntegrityError(f"destination {name!r} is not a regular file")
                digest = hashlib.sha256()
                size = 0
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
                if digest.hexdigest() != expected_hash.lower():
                    raise StorageIntegrityError(f"destination {name!r}: SHA-256 mismatch")
                if expected_size is not None and size != expected_size:
                    raise StorageIntegrityError(f"destination {name!r}: size mismatch")
        except OSError as exc:
            raise StorageIntegrityError(f"cannot verify destination {name!r}: {exc}") from exc


class LocalStorageBackend:
    def __init__(self, storage_root: str | Path):
        # Do not resolve away symlinks: walk every component with O_NOFOLLOW.
        self.storage_root = Path(os.path.abspath(storage_root))
        if self.storage_root == Path('/'):
            raise StorageInputError("storage_root cannot be the filesystem root")

    def store_package(self, metadata: AssetMetadataV1, normalized_asset: NormalizedAsset,
                      json_path: Path, observed_package_fingerprint: str) -> VerifiedStoredPackage:
        try:
            return self._store(metadata, normalized_asset, Path(json_path), observed_package_fingerprint)
        except (UnsafePackagePathError, ValueError) as exc:
            if isinstance(exc, StorageError):
                raise
            raise StorageInputError(f"invalid package input: {exc}") from exc
        except OSError as exc:
            raise StorageError(f"local custody failed under {self.storage_root}: {exc}") from exc

    def _store(self, metadata, asset, json_path, fingerprint):
        if fingerprint != fingerprint_for(asset):
            raise StorageInputError("observed_package_fingerprint does not match normalized_asset")
        # Structural consistency only: do not repeat semantic/trust validation.
        identity = (metadata.analysis.producer, metadata.asset.source_movie_id, metadata.asset.id)
        if identity != (asset.producer, asset.source_key, asset.producer_asset_id) or asset.asset_uid != atlas_asset_uid(*identity):
            raise StorageInputError("metadata identity does not match normalized_asset")
        for kind in ("horizontal", "vertical"):
            producer = getattr(metadata.media, kind)
            normalized = getattr(asset, kind)
            if (producer.file, producer.sha256, producer.size_bytes, producer.thumbnail.model_dump()) != (
                normalized.filename, normalized.sha256, normalized.size_bytes, normalized.thumbnail
            ):
                raise StorageInputError(f"metadata {kind} members do not match normalized_asset")
        if not isinstance(asset.asset_uid, uuid.UUID):
            raise StorageInputError("asset_uid must be a UUID")
        try:
            source_json = safe_package_path(json_path.parent, json_path.name)
            if not source_json.is_file():
                raise StorageInputError("JSON source must be a regular file")
            members = {}
            for kind in ("horizontal", "vertical"):
                rendition = getattr(metadata.media, kind)
                for item in (rendition, rendition.thumbnail):
                    source = safe_package_path(json_path.parent, item.file)
                    if not source.is_file():
                        raise StorageInputError(f"source {item.file!r} must be a regular file")
                    members[item.file] = (source, item.sha256, item.size_bytes)
            members[json_path.name] = (source_json, sha256_file(source_json), source_json.stat().st_size)
        except (UnsafePackagePathError, RuntimeError) as exc:
            if isinstance(exc, StorageError):
                raise
            raise StoragePathError(f"unsafe package source: {exc}") from exc
        if len(members) != 5 or len({item[0] for item in members.values()}) != 5:
            raise StorageInputError("package requires five distinct source files and filenames")
        if any(source.is_relative_to(self.storage_root) for source, _, _ in members.values()):
            raise StoragePathError("source package must be separate from Atlas storage_root")

        with ExitStack() as stack:
            root_fd = stack.enter_context(_directory(None, '/'))
            parts = self.storage_root.parts[1:]
            for part in parts:
                root_fd = stack.enter_context(_directory(root_fd, part, create=True))
            fcntl.flock(root_fd, fcntl.LOCK_EX)
            assets_fd = stack.enter_context(_directory(root_fd, 'assets', create=True))
            asset_fd = stack.enter_context(_directory(assets_fd, str(asset.asset_uid), create=True))
            try:
                existing_stat = os.stat(fingerprint, dir_fd=asset_fd, follow_symlinks=False)
                if not stat.S_ISDIR(existing_stat.st_mode) and not stat.S_ISLNK(existing_stat.st_mode):
                    raise StorageIntegrityError("existing version is not a package directory")
            except FileNotFoundError:
                exists = False
            else:
                exists = True
            if exists:
                with _directory(asset_fd, fingerprint) as final_fd:
                    _verify(final_fd, members)
                created = False
            else:
                staging_fd = stack.enter_context(_directory(root_fd, '.staging', create=True))
                stage_name = uuid.uuid4().hex
                os.mkdir(stage_name, mode=0o700, dir_fd=staging_fd)
                try:
                    with _directory(staging_fd, stage_name) as stage_fd:
                        for name, (source, _, _) in members.items():
                            with source.open('rb') as src:
                                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                             0o600, dir_fd=stage_fd)
                                with os.fdopen(fd, 'wb') as dst:
                                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                        _verify(stage_fd, members)
                    os.rename(stage_name, fingerprint, src_dir_fd=staging_fd, dst_dir_fd=asset_fd)
                except BaseException:
                    # Only this operation's unique Atlas staging directory.
                    shutil.rmtree(stage_name, dir_fd=staging_fd)
                    raise
                created = True

            base = self.storage_root / 'assets' / str(asset.asset_uid) / fingerprint
            def location(kind):
                rendition = getattr(metadata.media, kind)
                return VerifiedRenditionLocation((base / rendition.file).as_uri(),
                                                 (base / rendition.thumbnail.file).as_uri())
            return VerifiedStoredPackage(
                manifest=VerifiedStorageManifest(horizontal=location('horizontal'), vertical=location('vertical')),
                destination_verified_at=datetime.now(timezone.utc), destination_base_uri=base.as_uri(),
                metadata_uri=(base / json_path.name).as_uri(), package_fingerprint=fingerprint, created=created,
            )
