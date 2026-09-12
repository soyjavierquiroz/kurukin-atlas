"""Small synchronous storage interface and explicit settings factory."""

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from app.contracts.asset_metadata_v1 import AssetMetadataV1
from app.ingest.normalize import NormalizedAsset
from app.storage.contracts import VerifiedStoredPackage
from app.storage.errors import StorageConfigurationError

if TYPE_CHECKING:
    from app.settings import Settings


class StorageBackend(Protocol):
    def store_package(self, metadata: AssetMetadataV1, normalized_asset: NormalizedAsset,
                      json_path: Path, observed_package_fingerprint: str) -> VerifiedStoredPackage: ...


def get_storage_backend(settings: "Settings") -> StorageBackend:
    if settings.storage_backend == "local":
        from app.storage.local import LocalStorageBackend
        return LocalStorageBackend(settings.storage_root)
    if settings.storage_backend == "rclone_drive":
        from app.storage.rclone_drive import RcloneDriveStorageBackend
        return RcloneDriveStorageBackend(
            binary=settings.rclone_binary,
            remote=settings.rclone_remote,
            root=settings.rclone_root,
            lock_path=settings.rclone_lock_path,
        )
    raise StorageConfigurationError(f"unsupported storage backend: {settings.storage_backend!r}")
