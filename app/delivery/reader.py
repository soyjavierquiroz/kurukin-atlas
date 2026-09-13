"""Safe streaming readers for the two locator schemes emitted by Atlas custody."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Iterator, Protocol
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

from app.settings import Settings, get_settings
from app.storage.rclone import RcloneCommandRunner, SubprocessRcloneCommandRunner
from app.storage.rclone_drive import _validate_remote, _validate_root


_CHUNK_SIZE = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StoredObjectReadError(RuntimeError):
    """A deliberately detail-free failure crossing the storage-to-HTTP boundary."""


class StoredObjectReader(Protocol):
    def open(self, uri: str, *, offset: int = 0, count: int | None = None) -> Iterator[bytes]: ...


def _validate_window(offset: int, count: int | None) -> None:
    if not isinstance(offset, int) or offset < 0 or (count is not None and (not isinstance(count, int) or count < 0)):
        raise StoredObjectReadError("invalid read window")


class LocalStoredObjectReader:
    """Open `file://` locators below one Atlas-owned root using no-follow FDs."""

    def __init__(self, storage_root: str | Path) -> None:
        # Match LocalStorageBackend's established interpretation of a relative
        # configured root without changing the emitted file:// locator format.
        root = Path(os.path.abspath(storage_root))
        if not root.is_absolute() or root == Path("/"):
            raise ValueError("Atlas storage root must be a non-root absolute path")
        self.storage_root = root

    def _relative_parts(self, uri: str) -> tuple[str, ...]:
        try:
            parsed = urlsplit(uri)
        except ValueError as exc:
            raise StoredObjectReadError("malformed stored object locator") from exc
        if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
            raise StoredObjectReadError("unsupported stored object locator")
        if not parsed.path.startswith("/"):
            raise StoredObjectReadError("malformed stored object locator")
        decoded = unquote(parsed.path)
        candidate = Path(decoded)
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise StoredObjectReadError("malformed stored object locator")
        try:
            relative = candidate.relative_to(self.storage_root)
        except ValueError as exc:
            raise StoredObjectReadError("stored object is outside Atlas storage") from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise StoredObjectReadError("malformed stored object locator")
        # This is the immutable LocalStorageBackend layout, not a general
        # purpose file browser below storage_root.
        if len(relative.parts) != 4 or relative.parts[0] != "assets" or not _SHA256.fullmatch(relative.parts[2]):
            raise StoredObjectReadError("malformed stored object locator")
        try:
            UUID(relative.parts[1])
        except ValueError as exc:
            raise StoredObjectReadError("malformed stored object locator") from exc
        # Local custody uses Path.as_uri(); this rejects alternate/ambiguous URI spellings.
        if candidate.as_uri() != uri:
            raise StoredObjectReadError("malformed stored object locator")
        return relative.parts

    def open(self, uri: str, *, offset: int = 0, count: int | None = None) -> Iterator[bytes]:
        _validate_window(offset, count)
        parts = self._relative_parts(uri)

        def stream() -> Iterator[bytes]:
            flags = os.O_RDONLY | os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fds: list[int] = []
            try:
                root_fd = os.open(self.storage_root, flags)
                fds.append(root_fd)
                directory_fd = root_fd
                for part in parts[:-1]:
                    next_fd = os.open(part, flags, dir_fd=directory_fd)
                    fds.append(next_fd)
                    directory_fd = next_fd
                file_flags = os.O_RDONLY | os.O_NONBLOCK
                if hasattr(os, "O_NOFOLLOW"):
                    file_flags |= os.O_NOFOLLOW
                file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
                fds.append(file_fd)
                if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                    raise StoredObjectReadError("stored object is not a regular file")
                for fd in fds[:-1]:
                    os.close(fd)
                fds = [file_fd]
                os.lseek(file_fd, offset, os.SEEK_SET)
                remaining = count
                while remaining is None or remaining > 0:
                    chunk = os.read(file_fd, _CHUNK_SIZE if remaining is None else min(_CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    if remaining is not None:
                        remaining -= len(chunk)
                    yield chunk
            except (OSError, StoredObjectReadError) as exc:
                if isinstance(exc, StoredObjectReadError):
                    raise
                raise StoredObjectReadError("stored object could not be opened") from exc
            finally:
                for fd in reversed(fds):
                    try:
                        os.close(fd)
                    except OSError:
                        pass

        return stream()


class RcloneStoredObjectReader:
    """Stream canonical Atlas `rclone://` locators through argv-only rclone cat."""

    def __init__(self, *, binary: str, remote: str, root: str,
                 runner: RcloneCommandRunner | None = None, timeout_seconds: float = 120.0) -> None:
        if not isinstance(binary, str) or not binary.strip() or binary != binary.strip():
            raise ValueError("rclone binary must be non-empty")
        self.binary = binary
        self.remote = _validate_remote(remote)
        self.root = _validate_root(root)
        self.runner = runner or SubprocessRcloneCommandRunner()
        self.timeout_seconds = timeout_seconds

    def _remote_path(self, uri: str) -> str:
        try:
            parsed = urlsplit(uri)
        except ValueError as exc:
            raise StoredObjectReadError("malformed stored object locator") from exc
        if (parsed.scheme != "rclone" or parsed.netloc != self.remote[:-1] or parsed.query or parsed.fragment
                or not parsed.path.startswith("/")):
            raise StoredObjectReadError("unsupported stored object locator")
        encoded = parsed.path[1:]
        relative = unquote(encoded)
        path = PurePosixPath(relative)
        root_parts = PurePosixPath(self.root).parts
        remaining_parts = path.parts[len(root_parts):]
        if (not relative or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts)
                or path.parts[:len(root_parts)] != root_parts
                or len(remaining_parts) != 5 or remaining_parts[0] != "assets"
                or not _SHA256.fullmatch(remaining_parts[3].rsplit("--", 1)[-1])):
            raise StoredObjectReadError("stored object is outside Atlas storage")
        # Atlas URI construction has exactly this canonical encoding.  Checking it
        # means a corrupt DB URI cannot smuggle a second parsing interpretation.
        if quote(relative, safe="/-_.~") != encoded:
            raise StoredObjectReadError("malformed stored object locator")
        return f"{self.remote}{relative}"

    def open(self, uri: str, *, offset: int = 0, count: int | None = None) -> Iterator[bytes]:
        _validate_window(offset, count)
        remote_path = self._remote_path(uri)
        args: list[str] = [self.binary, "cat"]
        if offset:
            args.extend(("--offset", str(offset)))
        if count is not None:
            args.extend(("--count", str(count)))
        args.append(remote_path)
        try:
            # `stream` is itself a generator.  `yield from` ensures its finally
            # cleanup runs when StreamingResponse closes this iterator early.
            return self._stream(tuple(args))
        except Exception as exc:
            raise StoredObjectReadError("stored object could not be opened") from exc

    def _stream(self, args: tuple[str, ...]) -> Iterator[bytes]:
        try:
            yield from self.runner.stream(args, timeout_seconds=self.timeout_seconds)
        except Exception as exc:
            raise StoredObjectReadError("stored object could not be read") from exc


class _ConfiguredStoredObjectReader:
    """Dispatch catalog locators by scheme, independently of the write backend.

    ``storage_backend`` selects custody for new ingests only. Existing catalog
    records can legitimately refer to either supported custody scheme, so the
    reader configuration is a capability set, not a mirror of the writer.
    """

    def __init__(self, settings: Settings) -> None:
        self.local = LocalStoredObjectReader(settings.storage_root)
        self.rclone: RcloneStoredObjectReader | None = None
        # Settings require these values when rclone_drive is the active writer.
        # A complete valid configuration also authorizes reads of legacy rclone
        # catalog locators when local is the current writer. Invalid optional
        # values leave rclone unavailable but never disable local reads.
        binary = settings.rclone_binary
        if binary and binary.strip() and settings.rclone_remote and settings.rclone_root:
            try:
                self.rclone = RcloneStoredObjectReader(
                    binary=binary,
                    remote=settings.rclone_remote,
                    root=settings.rclone_root,
                )
            except ValueError:
                pass

    def open(self, uri: str, *, offset: int = 0, count: int | None = None) -> Iterator[bytes]:
        try:
            scheme = urlsplit(uri).scheme
        except ValueError as exc:
            raise StoredObjectReadError("malformed stored object locator") from exc
        if scheme == "file":
            return self.local.open(uri, offset=offset, count=count)
        if scheme == "rclone" and self.rclone is not None:
            return self.rclone.open(uri, offset=offset, count=count)
        raise StoredObjectReadError("unsupported stored object locator")


def get_stored_object_reader() -> StoredObjectReader:
    """FastAPI dependency factory; tests replace it rather than touching custody."""

    return _ConfiguredStoredObjectReader(get_settings())
