"""Storage-owned verified custody contracts; no catalog dependencies."""

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from app.ingest.normalize import SUPPORTED_RENDITION_KINDS
from app.storage.errors import StorageInputError


@dataclass(frozen=True)
class VerifiedRenditionLocation:
    """Already-verified final locations for one video rendition and its thumbnail."""

    storage_uri: str
    thumbnail_uri: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.storage_uri, str) or not self.storage_uri.strip():
            raise StorageInputError("storage_uri must be a non-empty string")
        if self.thumbnail_uri is not None and (
            not isinstance(self.thumbnail_uri, str) or not self.thumbnail_uri.strip()
        ):
            raise StorageInputError("thumbnail_uri must be None or a non-empty string")


@dataclass(frozen=True, init=False)
class VerifiedStorageManifest:
    """One or more verified rendition locations keyed by supported rendition kind."""

    renditions: Mapping[str, VerifiedRenditionLocation]

    def __init__(self, renditions: Mapping[str, VerifiedRenditionLocation] | None = None,
                 vertical: VerifiedRenditionLocation | None = None,
                 horizontal: VerifiedRenditionLocation | None = None) -> None:
        """Accept the mapping form, plus legacy explicit H/V keyword construction."""
        if isinstance(renditions, VerifiedRenditionLocation):
            # Backward-compatible positional H/V construction used by the
            # existing MBE-only local backend.
            if horizontal is not None:
                raise StorageInputError("ambiguous positional storage manifest locations")
            locations = {"horizontal": renditions}
            if vertical is not None:
                locations["vertical"] = vertical
        elif renditions is not None and (vertical is not None or horizontal is not None):
            raise StorageInputError("provide either rendition mapping or rendition keyword locations")
        elif renditions is not None:
            locations = dict(renditions)
        else:
            locations = {
                kind: location for kind, location in (("horizontal", horizontal), ("vertical", vertical)) if location is not None
            }
        if not locations:
            raise StorageInputError("storage manifest requires at least one rendition")
        if not set(locations).issubset(SUPPORTED_RENDITION_KINDS):
            raise StorageInputError("storage manifest contains an unsupported rendition kind")
        if not all(isinstance(location, VerifiedRenditionLocation) for location in locations.values()):
            raise StorageInputError("storage manifest rendition locations must be verified")
        object.__setattr__(self, "renditions", locations)

    @property
    def horizontal(self) -> VerifiedRenditionLocation | None:
        return self.renditions.get("horizontal")

    @property
    def vertical(self) -> VerifiedRenditionLocation | None:
        return self.renditions.get("vertical")


@dataclass(frozen=True)
class VerifiedStoredPackage:
    manifest: VerifiedStorageManifest
    destination_verified_at: datetime
    destination_base_uri: str
    metadata_uri: str
    package_fingerprint: str
    created: bool
