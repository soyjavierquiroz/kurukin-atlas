"""Small, deliberately narrow subprocess seam for rclone commands.

The Drive backend supplies every argument; this module never uses a shell and
never reads rclone configuration.  Tests replace ``RcloneCommandRunner`` with
an in-memory recording implementation.
"""

from __future__ import annotations

import os
import select
import subprocess
import time
from dataclasses import dataclass
from typing import Iterator, Protocol, Sequence

from app.storage.errors import RcloneTransportError


@dataclass(frozen=True)
class RcloneCommandResult:
    stdout: bytes
    stderr: bytes = b""


class RcloneCommandRunner(Protocol):
    def run(self, args: Sequence[str], *, timeout_seconds: float) -> RcloneCommandResult: ...

    def stream(self, args: Sequence[str], *, timeout_seconds: float) -> Iterator[bytes]: ...


class SubprocessRcloneCommandRunner:
    """Execute the already-approved rclone argument arrays owned by the backend."""

    def run(self, args: Sequence[str], *, timeout_seconds: float) -> RcloneCommandResult:
        try:
            completed = subprocess.run(
                list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False, timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise RcloneTransportError("rclone executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise RcloneTransportError("rclone command timed out") from exc
        except OSError as exc:
            raise RcloneTransportError("rclone command could not start") from exc
        if completed.returncode != 0:
            raise RcloneTransportError(f"rclone command failed (exit {completed.returncode})")
        return RcloneCommandResult(stdout=completed.stdout, stderr=completed.stderr)

    def stream(self, args: Sequence[str], *, timeout_seconds: float) -> Iterator[bytes]:
        """Yield stdout without buffering a remote media object in memory."""

        try:
            process = subprocess.Popen(
                list(args), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False,
            )
        except FileNotFoundError as exc:
            raise RcloneTransportError("rclone executable was not found") from exc
        except OSError as exc:
            raise RcloneTransportError("rclone command could not start") from exc

        assert process.stdout is not None
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                ready, _, _ = select.select([process.stdout], [], [], remaining)
                if not ready:
                    raise TimeoutError
                chunk = os.read(process.stdout.fileno(), 1024 * 1024)
                if not chunk:
                    break
                yield chunk
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            returncode = process.wait(timeout=remaining)
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            self._terminate(process)
            raise RcloneTransportError("rclone command timed out") from exc
        except OSError as exc:
            raise RcloneTransportError("rclone stream failed") from exc
        finally:
            if process.poll() is None:
                self._terminate(process)
        if returncode != 0:
            raise RcloneTransportError(f"rclone command failed (exit {returncode})")

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        """Best-effort cleanup that must not obscure the stream failure."""

        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            return
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
