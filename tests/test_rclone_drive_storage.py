"""Unit tests for Drive custody; the fake runner never invokes rclone or Drive."""

import hashlib
import json
from urllib.parse import unquote, urlsplit
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.contracts.asset_metadata_v1 import AssetMetadataV1
from app.contracts.curated_asset_v1 import CuratedAssetV1
from app.ingest.curated import canonical_curated_manifest, normalize_curated_asset
from app.ingest.fingerprint import observed_package_fingerprint
from app.ingest.normalize import normalize_metadata
from app.storage import (RcloneDriveCuratedStorageBackend, RcloneDriveStorageBackend,
                         StorageInputError, StorageIntegrityError, StoragePathError, get_storage_backend)
from app.storage.errors import RcloneTransportError
from app.storage.rclone import RcloneCommandResult
from app.storage.rclone_drive import encode_path_component


def package(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir(parents=True)
    media = {}
    for kind in ("horizontal", "vertical"):
        items = []
        for extension in ("mp4", "jpg"):
            name = f"{kind} file.{extension}"
            payload = f"{kind}-{extension}".encode()
            (source / name).write_bytes(payload)
            items.append({"file": name, "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)})
        media[kind] = {**items[0], "thumbnail": items[1], "technical_validated": True, "semantic_validated": True}
    raw = {"schema_version": "asset_metadata_v1", "analysis": {"producer": "movie_broll_extractor", "semantic_ready": True, "final_asset_semantics_validated": True},
           "asset": {"id": "rc162", "source_movie_id": "romper-el-circulo"}, "source": {"movie_sha256": "a" * 64},
           "editorial": {"decision": "keep"}, "visual": {}, "media": media}
    json_path = source / "rc162 metadata.json"
    json_path.write_bytes(json.dumps(raw, separators=(",", ":")).encode())
    metadata = AssetMetadataV1.model_validate(raw)
    return metadata, normalize_metadata(metadata, raw), json_path


class FakeRclone:
    """A tiny remote directory model with the rclone verbs this backend owns."""

    def __init__(self, *, missing_hash=False, extra_after_copy=False, move_fails=False, corrupt_after_move=False,
                 mkdir_fails=False):
        self.files: dict[str, bytes] = {}
        self.directories: set[str] = set()
        self.commands: list[tuple[str, ...]] = []
        self.missing_hash = missing_hash
        self.extra_after_copy = extra_after_copy
        self.move_fails = move_fails
        self.corrupt_after_move = corrupt_after_move
        self.mkdir_fails = mkdir_fails

    @staticmethod
    def _path(value: str) -> str:
        return value.split(":", 1)[1].strip("/")

    def _parents(self, path: str) -> None:
        parts = path.split("/")
        for index in range(1, len(parts)):
            self.directories.add("/".join(parts[:index]))

    def _listing(self, directory: str):
        if directory not in self.directories:
            raise RcloneTransportError("rclone command failed (exit 1)")
        prefix = directory.strip("/") + "/" if directory else ""
        children = []
        for candidate in sorted(self.directories):
            if candidate.startswith(prefix) and "/" not in candidate[len(prefix):]:
                children.append({"Name": candidate[len(prefix):], "IsDir": True, "Size": 0, "Hashes": {}})
        for candidate, payload in sorted(self.files.items()):
            if candidate.startswith(prefix) and "/" not in candidate[len(prefix):]:
                hashes = {} if self.missing_hash else {"SHA-256": hashlib.sha256(payload).hexdigest()}
                children.append({"Name": candidate[len(prefix):], "IsDir": False, "Size": len(payload), "Hashes": hashes})
        return children

    def run(self, args, *, timeout_seconds):
        command = args[1]
        self.commands.append(tuple(args))
        if command == "mkdir":
            if self.mkdir_fails:
                raise RcloneTransportError("rclone command failed (exit 1)")
            target = self._path(args[2])
            self._parents(target)
            self.directories.add(target)
            return RcloneCommandResult(b"")
        if command == "lsjson":
            return RcloneCommandResult(json.dumps(self._listing(self._path(args[2]))).encode())
        if command == "copyto":
            destination = self._path(args[3])
            if destination.rsplit("/", 1)[0] not in self.directories:
                raise RcloneTransportError("rclone command failed (exit 1)")
            self.files[destination] = Path(args[2]).read_bytes()
            self._parents(destination)
            if self.extra_after_copy:
                self.files[destination.rsplit("/", 1)[0] + "/unexpected.bin"] = b"extra"
            return RcloneCommandResult(b"")
        if command == "moveto":
            if self.move_fails:
                raise RcloneTransportError("rclone command failed (exit 1)")
            source, destination = self._path(args[2]), self._path(args[3])
            if source not in self.directories or destination.rsplit("/", 1)[0] not in self.directories:
                raise RcloneTransportError("rclone command failed (exit 1)")
            for path, payload in list(self.files.items()):
                if path.startswith(source + "/"):
                    self.files[destination + path[len(source):]] = payload
                    del self.files[path]
            self.directories.discard(source)
            self._parents(destination + "/member")
            self.directories.add(destination)
            if self.corrupt_after_move:
                victim = next(path for path in self.files if path.startswith(destination + "/"))
                self.files[victim] = b"X" * len(self.files[victim])
            return RcloneCommandResult(b"")
        if command == "purge":
            target = self._path(args[2])
            self.files = {path: data for path, data in self.files.items() if not path.startswith(target + "/")}
            self.directories = {path for path in self.directories if not (path == target or path.startswith(target + "/"))}
            return RcloneCommandResult(b"")
        raise AssertionError(command)

    def stream(self, args, *, timeout_seconds):
        self.commands.append(tuple(args))
        yield self.files[self._path(args[2])]


def backend(fake, tmp_path):
    return RcloneDriveStorageBackend(binary="rclone", remote="gdrive_javier:", root="Javier/KURUKIN_ATLAS",
                                     runner=fake, lock_path=tmp_path / "drive.lock")


def store(storage, inputs):
    metadata, asset, path = inputs
    return storage.store_package(metadata, asset, path, observed_package_fingerprint(asset))


def command_names(fake):
    return [command[1] for command in fake.commands]


def test_construction_and_factory_have_no_command_side_effects(tmp_path):
    fake = FakeRclone()
    storage = backend(fake, tmp_path)
    assert fake.commands == []
    configured = SimpleNamespace(storage_backend="rclone_drive", rclone_binary="rclone", rclone_remote="gdrive_javier:",
                                 rclone_root="Javier/KURUKIN_ATLAS", rclone_lock_path=tmp_path / "factory.lock")
    assert isinstance(get_storage_backend(configured), RcloneDriveStorageBackend)
    assert fake.commands == []
    assert not (tmp_path / "drive.lock").exists()


def test_encoding_and_rc162_final_path(tmp_path):
    assert encode_path_component("collection:deluxe") == "collection%3Adeluxe"
    with pytest.raises(StoragePathError):
        encode_path_component("a/b")
    with pytest.raises(StoragePathError):
        encode_path_component("..")
    _, asset, _ = package(tmp_path)
    fingerprint = observed_package_fingerprint(asset)
    assert backend(FakeRclone(), tmp_path)._final_dir(asset, fingerprint) == (
        f"Javier/KURUKIN_ATLAS/assets/movie_broll_extractor/romper-el-circulo/rc162--{fingerprint}"
    )


def test_uri_round_trip_preserves_physical_percent_encoded_identity_and_literal_percent(tmp_path):
    storage = backend(FakeRclone(), tmp_path)
    physical = "Javier/KURUKIN_ATLAS/assets/kurukin_curated/collection%3Adeluxe/x y.mp4"
    uri = storage._uri(physical)
    assert "collection%253Adeluxe" in uri and uri.endswith("x%20y.mp4")
    assert unquote(urlsplit(uri).path.lstrip("/")) == physical
    literal_percent = "Javier/KURUKIN_ATLAS/assets/kurukin_curated/source/x% y.mp4"
    assert unquote(urlsplit(storage._uri(literal_percent)).path.lstrip("/")) == literal_percent


def test_new_publish_uses_exact_copyto_members_then_move_and_returns_h_and_v(tmp_path):
    inputs, fake = package(tmp_path), FakeRclone()
    result = store(backend(fake, tmp_path), inputs)
    copy_commands = [command for command in fake.commands if command[1] == "copyto"]
    assert len(copy_commands) == 5
    assert all(command[2].startswith(str(inputs[2].parent)) for command in copy_commands)
    assert "copy" not in command_names(fake)
    assert command_names(fake).count("moveto") == 1
    assert result.created is True
    assert result.manifest.horizontal.thumbnail_uri.endswith("horizontal%20file.jpg")
    assert result.manifest.vertical.thumbnail_uri.endswith("vertical%20file.jpg")
    assert result.destination_base_uri.startswith("rclone://gdrive_javier/Javier/KURUKIN_ATLAS/assets/")
    assert result.destination_verified_at.utcoffset() is not None
    assert "purge" not in command_names(fake)
    final_parent = "Javier/KURUKIN_ATLAS/assets/movie_broll_extractor/romper-el-circulo"
    mkdirs = [FakeRclone._path(command[2]) for command in fake.commands if command[1] == "mkdir"]
    assert mkdirs[0] == final_parent
    assert len(mkdirs) == 2 and mkdirs[1].startswith("Javier/KURUKIN_ATLAS/_staging/")


@pytest.mark.parametrize("kind", ["extra", "missing", "hash", "size"])
def test_remote_staging_must_match_exact_members_and_bytes(tmp_path, kind):
    inputs, fake = package(tmp_path), FakeRclone(extra_after_copy=kind == "extra")
    if kind == "missing":
        original = fake.run
        def remove_after_copy(args, *, timeout_seconds):
            result = original(args, timeout_seconds=timeout_seconds)
            if args[1] == "copyto" and len([item for item in fake.commands if item[1] == "copyto"]) == 5:
                del fake.files[next(iter(fake.files))]
            return result
        fake.run = remove_after_copy
    elif kind in {"hash", "size"}:
        original = fake.run
        def damage_after_copy(args, *, timeout_seconds):
            result = original(args, timeout_seconds=timeout_seconds)
            if args[1] == "copyto" and len([item for item in fake.commands if item[1] == "copyto"]) == 5:
                path = next(iter(fake.files))
                fake.files[path] = (b"bad" if kind == "size" else b"X" * len(fake.files[path]))
            return result
        fake.run = damage_after_copy
    with pytest.raises(StorageIntegrityError):
        store(backend(fake, tmp_path), inputs)
    assert "purge" in command_names(fake)
    assert "moveto" not in command_names(fake)


def test_existing_exact_final_replays_without_upload_and_corrupt_final_never_repairs(tmp_path):
    inputs, fake = package(tmp_path), FakeRclone()
    first = store(backend(fake, tmp_path), inputs)
    fake.commands.clear()
    replay = store(backend(fake, tmp_path), inputs)
    assert not replay.created and command_names(fake) == ["mkdir", "lsjson", "lsjson"]
    final_file = next(path for path in fake.files if path.startswith(first.destination_base_uri.split("gdrive_javier/", 1)[1]))
    fake.files[final_file] = b"bad"
    fake.commands.clear()
    with pytest.raises(StorageIntegrityError):
        store(backend(fake, tmp_path), inputs)
    assert "copyto" not in command_names(fake) and "purge" not in command_names(fake)


def test_mkdir_failure_prevents_listing_copy_and_move(tmp_path):
    inputs, fake = package(tmp_path), FakeRclone(mkdir_fails=True)
    with pytest.raises(RcloneTransportError):
        store(backend(fake, tmp_path), inputs)
    assert command_names(fake) == ["mkdir"]


def test_move_failure_purges_only_staging_and_final_verify_failure_does_not_purge(tmp_path):
    inputs, failed_move = package(tmp_path), FakeRclone(move_fails=True)
    with pytest.raises(RcloneTransportError):
        store(backend(failed_move, tmp_path), inputs)
    assert command_names(failed_move)[-1] == "purge"
    assert not any("/assets/" in command[-1] for command in failed_move.commands if command[1] == "purge")
    inputs, bad_final = package(tmp_path / "other"), FakeRclone(corrupt_after_move=True)
    with pytest.raises(StorageIntegrityError):
        store(backend(bad_final, tmp_path / "other"), inputs)
    assert "moveto" in command_names(bad_final)
    assert "purge" not in command_names(bad_final)


def test_purge_refuses_non_uuid_or_non_staging_paths(tmp_path):
    fake = FakeRclone()
    storage = backend(fake, tmp_path)
    with pytest.raises(StoragePathError, match="UUID"):
        storage._purge_staging("Javier/KURUKIN_ATLAS/assets/not-staging")
    assert fake.commands == []


def test_missing_sha256_streams_rclone_cat(tmp_path):
    inputs, fake = package(tmp_path), FakeRclone(missing_hash=True)
    store(backend(fake, tmp_path), inputs)
    assert command_names(fake).count("cat") == 10  # staging and final, five files each


def test_source_and_fingerprint_identity_rejections_precede_commands(tmp_path):
    metadata, asset, path = package(tmp_path)
    fake = FakeRclone()
    with pytest.raises(StorageInputError, match="fingerprint"):
        backend(fake, tmp_path).store_package(metadata, asset, path, "B" * 64)
    changed = type(asset)(**{**asset.__dict__, "producer": "different"})
    with pytest.raises(StorageInputError, match="metadata"):
        backend(fake, tmp_path).store_package(metadata, changed, path, observed_package_fingerprint(changed))
    (path.parent / metadata.media.horizontal.file).unlink()
    (path.parent / metadata.media.horizontal.file).symlink_to(path)
    with pytest.raises(StoragePathError):
        store(backend(fake, tmp_path), (metadata, asset, path))
    assert fake.commands == []


def test_source_producer_json_must_semantically_match_normalized_asset_before_commands(tmp_path):
    metadata, asset, path = package(tmp_path)
    changed = json.loads(path.read_text())
    changed["asset"]["id"] = "different-but-valid"
    path.write_text(json.dumps(changed, indent=2))
    fake = FakeRclone()
    with pytest.raises(StorageInputError, match="does not match"):
        store(backend(fake, tmp_path), (metadata, asset, path))
    assert fake.commands == []


def test_source_producer_json_allows_different_whitespace(tmp_path):
    metadata, asset, path = package(tmp_path)
    path.write_text(json.dumps(asset.raw_producer_metadata, indent=2))
    assert store(backend(FakeRclone(), tmp_path), (metadata, asset, path)).created


def test_writer_lock_rejects_a_symlink(tmp_path):
    target = tmp_path / "target.lock"
    target.write_text("not a lock")
    lock = tmp_path / "locks" / "drive.lock"
    lock.parent.mkdir()
    lock.symlink_to(target)
    with pytest.raises(StoragePathError, match="writer lock"):
        storage = RcloneDriveStorageBackend(binary="rclone", remote="gdrive_javier:", root="Javier/KURUKIN_ATLAS",
                                            runner=FakeRclone(), lock_path=lock)
        store(storage, package(tmp_path / "source"))


def test_malformed_json_timeout_and_missing_executable_are_transport_errors(tmp_path):
    class BadJson(FakeRclone):
        def run(self, args, *, timeout_seconds):
            self.commands.append(tuple(args))
            return RcloneCommandResult(b"not-json")
    with pytest.raises(RcloneTransportError, match="malformed"):
        store(backend(BadJson(), tmp_path), package(tmp_path))

    class TimedOut(FakeRclone):
        def run(self, args, *, timeout_seconds):
            raise RcloneTransportError("rclone command timed out")
    with pytest.raises(RcloneTransportError, match="timed out"):
        store(backend(TimedOut(), tmp_path / "timeout"), package(tmp_path / "timeout"))

    class Missing(FakeRclone):
        def run(self, args, *, timeout_seconds):
            raise RcloneTransportError("rclone executable was not found")
    with pytest.raises(RcloneTransportError, match="not found"):
        store(backend(Missing(), tmp_path / "missing"), package(tmp_path / "missing"))


def test_curated_vertical_publish_uses_two_members_and_exact_replay(tmp_path):
    source = tmp_path / "deluxe_0001.mp4"
    source.write_bytes(b"curated-video")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    metadata = CuratedAssetV1.model_validate({
        "schema_version": "curated_asset_v1", "collection_id": "deluxe", "producer_asset_id": "deluxe_0001",
        "renditions": [{"kind": "vertical", "filename": source.name, "sha256": digest,
                        "size_bytes": source.stat().st_size, "technical_validated": True,
                        "semantic_validated": False, "thumbnail": {}}],
    })
    asset = normalize_curated_asset(metadata, metadata.model_dump(mode="json"))
    manifest = tmp_path / "atlas-curated.json"
    manifest.write_bytes(canonical_curated_manifest(metadata))
    fake = FakeRclone()
    storage = RcloneDriveCuratedStorageBackend(binary="rclone", remote="gdrive_javier:", root="Javier/KURUKIN_ATLAS",
                                                runner=fake, lock_path=tmp_path / "drive.lock")
    fingerprint = observed_package_fingerprint(asset)
    first = storage.store_curated_asset(metadata, asset, source, manifest, fingerprint)
    copied = [command for command in fake.commands if command[1] == "copyto"]
    assert len(copied) == 2
    assert {Path(command[3]).name for command in copied} == {source.name, "atlas-curated.json"}
    final_dir = unquote(urlsplit(first.destination_base_uri).path.lstrip("/"))
    final_members = {Path(path).name: payload for path, payload in fake.files.items() if path.startswith(final_dir + "/")}
    assert set(final_members) == {source.name, "atlas-curated.json"}
    assert json.loads(final_members["atlas-curated.json"]) == metadata.model_dump(mode="json")
    assert any("collection%3Adeluxe" in " ".join(command) for command in fake.commands)
    assert first.created and first.manifest.renditions.keys() == {"vertical"}
    assert first.manifest.vertical.thumbnail_uri is None
    assert "collection%253Adeluxe" in first.destination_base_uri
    fake.commands.clear()
    replay = storage.store_curated_asset(metadata, asset, source, manifest, fingerprint)
    assert not replay.created
    assert command_names(fake) == ["mkdir", "lsjson", "lsjson"]


def test_curated_source_byte_mismatch_is_rejected_before_any_rclone_command(tmp_path):
    source = tmp_path / "nature_0001.mp4"
    original = b"original curated bytes"
    replacement = b"replacement-curatedxxx"  # deliberately the same length
    assert len(original) == len(replacement)
    source.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    metadata = CuratedAssetV1.model_validate({
        "schema_version": "curated_asset_v1", "collection_id": "nature", "producer_asset_id": "nature_0001",
        "renditions": [{"kind": "vertical", "filename": source.name, "sha256": digest,
                        "size_bytes": len(original), "thumbnail": {}}],
    })
    asset = normalize_curated_asset(metadata, metadata.model_dump(mode="json"))
    manifest = tmp_path / "atlas-curated.json"
    manifest.write_bytes(canonical_curated_manifest(metadata))
    source.write_bytes(replacement)
    fake = FakeRclone()
    storage = RcloneDriveCuratedStorageBackend(binary="rclone", remote="gdrive_javier:", root="Javier/KURUKIN_ATLAS",
                                                runner=fake, lock_path=tmp_path / "drive.lock")
    with pytest.raises(StorageIntegrityError, match="source bytes"):
        storage.store_curated_asset(metadata, asset, source, manifest, observed_package_fingerprint(asset))
    assert fake.commands == []


def test_curated_source_symlink_replacement_is_rejected_before_any_rclone_command(tmp_path):
    source = tmp_path / "nature_0001.mp4"
    source.write_bytes(b"curated source bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    metadata = CuratedAssetV1.model_validate({
        "schema_version": "curated_asset_v1", "collection_id": "nature", "producer_asset_id": "nature_0001",
        "renditions": [{"kind": "vertical", "filename": source.name, "sha256": digest,
                        "size_bytes": source.stat().st_size, "thumbnail": {}}],
    })
    asset = normalize_curated_asset(metadata, metadata.model_dump(mode="json"))
    manifest = tmp_path / "atlas-curated.json"
    manifest.write_bytes(canonical_curated_manifest(metadata))
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(replacement)
    fake = FakeRclone()
    storage = RcloneDriveCuratedStorageBackend(binary="rclone", remote="gdrive_javier:", root="Javier/KURUKIN_ATLAS",
                                                runner=fake, lock_path=tmp_path / "drive.lock")
    with pytest.raises(StorageIntegrityError, match="safely rebound"):
        storage.store_curated_asset(metadata, asset, source, manifest, observed_package_fingerprint(asset))
    assert fake.commands == []
