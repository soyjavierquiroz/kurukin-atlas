"""Unit tests for the subprocess-only rclone command seam."""

import subprocess
import sys

import pytest

from app.storage.errors import RcloneTransportError
from app.storage.rclone import SubprocessRcloneCommandRunner


def test_stream_discards_stderr_while_preserving_stdout(monkeypatch):
    original_popen = subprocess.Popen
    observed = {}

    def popen(args, **kwargs):
        observed["stderr"] = kwargs["stderr"]
        observed["shell"] = kwargs["shell"]
        return original_popen(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'payload'); sys.stderr.write('x' * 100000)"],
            **kwargs,
        )

    monkeypatch.setattr("app.storage.rclone.subprocess.Popen", popen)
    assert b"".join(SubprocessRcloneCommandRunner().stream(("rclone", "cat", "remote:file"), timeout_seconds=2)) == b"payload"
    assert observed["stderr"] is subprocess.DEVNULL
    assert observed["shell"] is False


def test_stream_terminates_subprocess_when_consumer_closes(monkeypatch):
    original_popen = subprocess.Popen
    observed = {}

    def popen(args, **kwargs):
        process = original_popen(
            [sys.executable, "-c", "import sys, time; sys.stdout.buffer.write(b'x'); sys.stdout.flush(); time.sleep(60)"],
            **kwargs,
        )
        observed["process"] = process
        return process

    monkeypatch.setattr("app.storage.rclone.subprocess.Popen", popen)
    stream = SubprocessRcloneCommandRunner().stream(("rclone", "cat", "remote:file"), timeout_seconds=2)
    assert next(stream) == b"x"
    stream.close()
    assert observed["process"].poll() is not None


def test_stream_reaps_subprocess_on_normal_eof_and_failure(monkeypatch):
    original_popen = subprocess.Popen
    observed = []

    def popen(args, **kwargs):
        command = (
            "import sys; sys.stdout.buffer.write(b'ok')"
            if not observed else
            "import sys; sys.stderr.write('private failure'); sys.exit(7)"
        )
        process = original_popen([sys.executable, "-c", command], **kwargs)
        observed.append(process)
        return process

    monkeypatch.setattr("app.storage.rclone.subprocess.Popen", popen)
    runner = SubprocessRcloneCommandRunner()
    assert b"".join(runner.stream(("rclone", "cat", "remote:ok"), timeout_seconds=2)) == b"ok"
    with pytest.raises(RcloneTransportError, match="exit 7"):
        list(runner.stream(("rclone", "cat", "remote:bad"), timeout_seconds=2))
    assert all(process.poll() is not None for process in observed)
