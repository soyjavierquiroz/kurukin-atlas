"""Unit tests for the subprocess-only rclone command seam."""

import subprocess
import sys

from app.storage.rclone import SubprocessRcloneCommandRunner


def test_stream_discards_stderr_while_preserving_stdout(monkeypatch):
    original_popen = subprocess.Popen
    observed = {}

    def popen(args, **kwargs):
        observed["stderr"] = kwargs["stderr"]
        return original_popen(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'payload'); sys.stderr.write('x' * 100000)"],
            **kwargs,
        )

    monkeypatch.setattr("app.storage.rclone.subprocess.Popen", popen)
    assert b"".join(SubprocessRcloneCommandRunner().stream(("rclone", "cat", "remote:file"), timeout_seconds=2)) == b"payload"
    assert observed["stderr"] is subprocess.DEVNULL
