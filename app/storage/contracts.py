"""Storage-owned verified custody contracts; no catalog dependencies."""

from dataclasses import dataclass
from datetime import datetime

from app.storage.errors import StorageInputError


@dataclass(frozen=True)
class VerifiedRenditionLocation:
    """Already-verified final locations for one video rendition and its thumbnail."""

    storage_uri: str
    thumbnail_uri: str

    def __post_init__(self) -> None:
        if not isinstance(self.storage_uri, str) or not self.storage_uri.strip():
            raise StorageInputError("storage_uri must be a non-empty string")
        if not isinstance(self.thumbnail_uri, str) or not self.thumbnail_uri.strip():
            raise StorageInputError("thumbnail_uri must be a non-empty string")


@dataclass(frozen=True)
class VerifiedStorageManifest:
    """The exactly four verified locators required for the two Atlas renditions."""

    horizontal: VerifiedRenditionLocation
    vertical: VerifiedRenditionLocation

    def __post_init__(self) -> None:
        if not isinstance(self.horizontal, VerifiedRenditionLocation) or not isinstance(self.vertical, VerifiedRenditionLocation):
            raise StorageInputError("storage manifest requires horizontal and vertical verified locations")


@dataclass(frozen=True)
class VerifiedStoredPackage:
    manifest: VerifiedStorageManifest
    destination_verified_at: datetime
    destination_base_uri: str
    metadata_uri: str
    package_fingerprint: str
    created: bool
