"""Storage reader tests: locator boundaries and streaming lifecycle only."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.delivery.reader import (
    LocalStoredObjectReader,
    RcloneStoredObjectReader,
    StoredObjectReadError,
    _ConfiguredStoredObjectReader,
)
from app.api.main import _open_stream


def test_local_reader_streams_bounded_bytes_and_rejects_escape(tmp_path):
    root = tmp_path / "atlas"
    member = root / "assets" / str(uuid4()) / ("a" * 64) / "video.mp4"
    member.parent.mkdir(parents=True)
    member.write_bytes(b"abcdef")
    reader = LocalStoredObjectReader(root)
    assert b"".join(reader.open(member.as_uri(), offset=1, count=3)) == b"bcd"

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"private")
    with pytest.raises(StoredObjectReadError):
        reader.open(outside.as_uri())
    with pytest.raises(StoredObjectReadError):
        reader.open(f"file://{root}/assets/asset/../video.mp4")
    link = member.parent / "link.mp4"
    link.symlink_to(outside)
    with pytest.raises(StoredObjectReadError):
        list(reader.open(link.as_uri()))


def test_rclone_reader_only_accepts_canonical_atlas_locator_and_closes_stream():
    calls = []
    closed = []

    class Runner:
        def stream(self, args, *, timeout_seconds):
            calls.append((tuple(args), timeout_seconds))
            try:
                yield b"first"
                yield b"second"
            finally:
                closed.append(True)

    reader = RcloneStoredObjectReader(binary="rclone", remote="atlas:", root="KURUKIN_ATLAS", runner=Runner())
    uri = "rclone://atlas/KURUKIN_ATLAS/assets/mbe/source/asset--" + "a" * 64 + "/member.mp4"
    stream = reader.open(uri, offset=4, count=9)
    assert next(stream) == b"first"
    stream.close()
    assert closed == [True]
    assert calls == [(('rclone', 'cat', '--offset', '4', '--count', '9', 'atlas:KURUKIN_ATLAS/assets/mbe/source/asset--' + 'a' * 64 + '/member.mp4'), 120.0)]

    for bad in (
        "rclone://other/KURUKIN_ATLAS/assets/mbe/source/asset--" + "a" * 64 + "/member.mp4",
        "rclone://atlas/KURUKIN_ATLAS/_staging/member.mp4",
        "rclone://atlas/KURUKIN_ATLAS/assets/%2E%2E/private.mp4",
        "rclone://atlas/KURUKIN_ATLAS/assets/a%2Fb.mp4",
    ):
        with pytest.raises(StoredObjectReadError):
            reader.open(bad)


def test_configured_reader_keeps_rclone_read_capability_when_local_is_writer(tmp_path):
    class Runner:
        def stream(self, args, *, timeout_seconds):
            yield b"remote bytes"

    settings = SimpleNamespace(
        storage_backend="local",
        storage_root=str(tmp_path),
        rclone_binary="rclone",
        rclone_remote="atlas:",
        rclone_root="KURUKIN_ATLAS",
    )
    reader = _ConfiguredStoredObjectReader(settings)
    assert reader.rclone is not None
    reader.rclone.runner = Runner()
    uri = "rclone://atlas/KURUKIN_ATLAS/assets/mbe/source/asset--" + "a" * 64 + "/member.mp4"
    assert b"".join(reader.open(uri)) == b"remote bytes"


def test_configured_reader_keeps_local_read_capability_when_rclone_is_writer(tmp_path):
    member = tmp_path / "assets" / str(uuid4()) / ("a" * 64) / "video.mp4"
    member.parent.mkdir(parents=True)
    member.write_bytes(b"local bytes")
    settings = SimpleNamespace(
        storage_backend="rclone_drive",
        storage_root=str(tmp_path),
        rclone_binary="rclone",
        rclone_remote="atlas:",
        rclone_root="KURUKIN_ATLAS",
    )
    reader = _ConfiguredStoredObjectReader(settings)
    assert b"".join(reader.open(member.as_uri())) == b"local bytes"


def test_late_rclone_failure_is_sanitized_and_closes_its_runner_stream():
    closed = []

    class Runner:
        def stream(self, args, *, timeout_seconds):
            try:
                yield b"first"
                raise RuntimeError("rclone://private/path stderr")
            finally:
                closed.append(True)

    reader = RcloneStoredObjectReader(binary="rclone", remote="atlas:", root="KURUKIN_ATLAS", runner=Runner())
    uri = "rclone://atlas/KURUKIN_ATLAS/assets/mbe/source/asset--" + "a" * 64 + "/member.mp4"
    stream = _open_stream(reader, uri, expected_size=6)
    assert next(stream) == b"first"
    with pytest.raises(StoredObjectReadError) as error:
        next(stream)
    assert "private" not in str(error.value)
    assert closed == [True]
