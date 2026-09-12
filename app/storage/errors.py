"""Explicit storage handoff failures."""


class StorageError(RuntimeError):
    """Storage could not complete the handoff."""


class StorageInputError(StorageError, ValueError):
    """Invalid or inconsistent storage input."""


class StorageIntegrityError(StorageError):
    """Destination bytes or package completeness do not match expectations."""


class StoragePathError(StorageError):
    """An unsafe source or destination path was rejected."""


class StorageConfigurationError(StorageInputError):
    """The configured storage backend is unsupported."""


class RcloneTransportError(StorageError):
    """The controlled rclone transport could not complete an operation."""
