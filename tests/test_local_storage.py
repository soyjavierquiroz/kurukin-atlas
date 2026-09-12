"""Synthetic custody tests; no real media, database, or producer outbox access."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import pytest

from app.contracts.asset_metadata_v1 import AssetMetadataV1
from app.ingest.fingerprint import observed_package_fingerprint
from app.ingest.normalize import normalize_metadata
from app.storage import (
    LocalStorageBackend, StorageConfigurationError, StorageError, StorageInputError,
    StorageIntegrityError, StoragePathError, get_storage_backend,
)
from app.storage import local


def package(tmp_path, revision=b'one'):
    source = tmp_path / 'source'
    source.mkdir(exist_ok=True)
    media = {}
    for kind in ('horizontal', 'vertical'):
        items = []
        for extension in ('mp4', 'jpg'):
            name = f'{kind} space.{extension}'
            data = kind.encode() + extension.encode() + revision
            (source / name).write_bytes(data)
            items.append(dict(file=name, sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data)))
        media[kind] = dict(**items[0], thumbnail=items[1], technical_validated=True, semantic_validated=True)
    raw = dict(schema_version='asset_metadata_v1',
               analysis=dict(producer='mbe', semantic_ready=True, final_asset_semantics_validated=True),
               asset=dict(id='asset-1', source_movie_id='movie-1'), source=dict(movie_sha256='a' * 64),
               editorial=dict(decision='keep'), visual={}, media=media)
    path = source / 'producer metadata.json'
    path.write_bytes((' \n' + json.dumps(raw, indent=3) + '\n\n').encode())
    metadata = AssetMetadataV1.model_validate(raw)
    asset = normalize_metadata(metadata, raw)
    return metadata, asset, path


def store(backend, inputs):
    metadata, asset, path = inputs
    return backend.store_package(metadata, asset, path, observed_package_fingerprint(asset))


def uri_path(uri):
    parsed = urlparse(uri)
    assert parsed.scheme == 'file' and parsed.netloc == ''
    path = Path(unquote(parsed.path))
    assert path.is_absolute()
    return path


def snapshot(path):
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) for p in path.iterdir()}


def test_success_and_idempotent_replay(tmp_path):
    inputs = package(tmp_path)
    before = snapshot(inputs[2].parent)
    root = tmp_path / 'storage'
    backend = LocalStorageBackend(root)
    first = store(backend, inputs)
    second = store(backend, inputs)
    assert first.created and not second.created
    assert first.manifest == second.manifest
    assert first.metadata_uri == second.metadata_uri
    assert first.destination_verified_at.utcoffset() is not None
    assert second.destination_verified_at >= first.destination_verified_at
    base = uri_path(first.destination_base_uri)
    assert base == root / 'assets' / str(inputs[1].asset_uid) / observed_package_fingerprint(inputs[1])
    assert len(list(base.parent.iterdir())) == 1
    assert len(list(base.iterdir())) == 5
    for kind in ('horizontal', 'vertical'):
        loc = getattr(first.manifest, kind)
        assert loc.thumbnail_uri is not None
        expected = getattr(inputs[0].media, kind)
        for uri, item in ((loc.storage_uri, expected), (loc.thumbnail_uri, expected.thumbnail)):
            dest = uri_path(uri)
            assert dest.is_relative_to(root)
            assert not dest.is_relative_to(inputs[2].parent)
            assert hashlib.sha256(dest.read_bytes()).hexdigest() == item.sha256
            assert dest.stat().st_size == item.size_bytes
    assert uri_path(first.metadata_uri).read_bytes() == inputs[2].read_bytes()
    assert snapshot(inputs[2].parent) == before
    assert list((root / '.staging').iterdir()) == []


def test_store_creates_missing_nested_storage_root(tmp_path):
    inputs = package(tmp_path)
    root = tmp_path / 'missing' / 'nested' / 'storage'
    backend = LocalStorageBackend(root)
    for directory in (root.parent.parent, root.parent, root):
        assert not directory.exists()

    result = store(backend, inputs)

    assert result.created
    base = uri_path(result.destination_base_uri)
    assert base == root / 'assets' / str(inputs[1].asset_uid) / observed_package_fingerprint(inputs[1])
    sources = {p.name: p for p in inputs[2].parent.iterdir()}
    assert len(sources) == 5
    assert {p.name for p in base.iterdir()} == set(sources)
    for name, source in sources.items():
        destination = base / name
        assert destination.stat().st_size == source.stat().st_size
        assert hashlib.sha256(destination.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest()
    assert list((root / '.staging').iterdir()) == []


def test_revision_isolation(tmp_path):
    backend = LocalStorageBackend(tmp_path / 'storage')
    first = store(backend, package(tmp_path))
    before = snapshot(uri_path(first.destination_base_uri))
    second = store(backend, package(tmp_path, b'two'))
    assert second.created and first.package_fingerprint != second.package_fingerprint
    assert uri_path(first.destination_base_uri).parent == uri_path(second.destination_base_uri).parent
    assert snapshot(uri_path(first.destination_base_uri)) == before


@pytest.mark.parametrize('failure', ['hash', 'size', 'copy', 'json'])
def test_failed_copy_or_verification_never_publishes_or_mutates_source(tmp_path, monkeypatch, failure):
    metadata, asset, path = package(tmp_path)
    if failure in ('hash', 'size'):
        if failure == 'hash':
            metadata.media.horizontal.sha256 = '0' * 64
        else:
            metadata.media.horizontal.size_bytes += 1
        asset = normalize_metadata(metadata, metadata.model_dump())
    original_copy = local.shutil.copyfileobj
    def bad_copy(src, dst, **kwargs):
        if failure == 'copy':
            raise OSError('injected copy failure')
        original_copy(src, dst, **kwargs)
        if src.name == str(path):
            dst.write(b'corruption')
    if failure in ('copy', 'json'):
        monkeypatch.setattr(local.shutil, 'copyfileobj', bad_copy)
    before = snapshot(path.parent)
    root = tmp_path / 'storage'
    with pytest.raises(StorageError if failure == 'copy' else StorageIntegrityError):
        store(LocalStorageBackend(root), (metadata, asset, path))
    assert snapshot(path.parent) == before
    assert list((root / '.staging').iterdir()) == []
    assert list((root / 'assets' / str(asset.asset_uid)).iterdir()) == []


@pytest.mark.parametrize('damage', ['corrupt', 'missing', 'extra', 'symlink'])
def test_existing_integrity_incident_is_not_repaired(tmp_path, damage):
    inputs = package(tmp_path)
    backend = LocalStorageBackend(tmp_path / 'storage')
    result = store(backend, inputs)
    base = uri_path(result.destination_base_uri)
    victim = uri_path(result.metadata_uri)
    if damage == 'corrupt':
        victim.write_bytes(b'corrupt')
    elif damage == 'missing':
        victim.unlink()
    elif damage == 'extra':
        (base / 'extra').write_bytes(b'extra')
    else:
        victim.unlink()
        victim.symlink_to(inputs[2])
    before = snapshot(inputs[2].parent)
    listing = {p.name: p.lstat() for p in base.iterdir()}
    with pytest.raises(StorageIntegrityError):
        store(backend, inputs)
    assert {p.name: p.lstat() for p in base.iterdir()} == listing
    assert snapshot(inputs[2].parent) == before


@pytest.mark.parametrize('component', ['parent', 'root', 'assets', 'asset', 'version', 'staging'])
def test_destination_symlink_escape(tmp_path, component):
    inputs = package(tmp_path)
    root = tmp_path / 'storage'
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'sentinel').write_bytes(b'untouched')
    if component == 'parent':
        root = root / 'nested' / 'storage'
    paths = dict(parent=root.parent.parent, root=root, assets=root / 'assets', asset=root / 'assets' / str(inputs[1].asset_uid),
                 version=root / 'assets' / str(inputs[1].asset_uid) / observed_package_fingerprint(inputs[1]),
                 staging=root / '.staging')
    target = paths[component]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)
    before = snapshot(outside)
    with pytest.raises(StoragePathError):
        store(LocalStorageBackend(root), inputs)
    assert snapshot(outside) == before


@pytest.mark.parametrize('bad_path', ['absolute', 'traversal', 'symlink', 'json_symlink', 'json_directory'])
def test_unsafe_sources(tmp_path, bad_path):
    metadata, asset, path = package(tmp_path)
    outside = tmp_path / 'outside'
    outside.write_bytes(b'outside')
    if bad_path == 'absolute':
        metadata.media.horizontal.file = str(outside)
    elif bad_path == 'traversal':
        metadata.media.horizontal.file = '../outside'
    elif bad_path == 'symlink':
        member = path.parent / metadata.media.horizontal.file
        member.unlink()
        member.symlink_to(outside)
    elif bad_path == 'json_symlink':
        path.unlink()
        path.symlink_to(outside)
    else:
        path = path.parent
    asset = normalize_metadata(metadata, metadata.model_dump())
    with pytest.raises((StoragePathError, StorageInputError)):
        store(LocalStorageBackend(tmp_path / 'storage'), (metadata, asset, path))
    assert outside.read_bytes() == b'outside'
    assert not (tmp_path / 'storage').exists()


def test_input_fingerprint_and_metadata_consistency(tmp_path):
    metadata, asset, path = package(tmp_path)
    backend = LocalStorageBackend(tmp_path / 'storage')
    with pytest.raises(StorageInputError, match='fingerprint'):
        backend.store_package(metadata, asset, path, 'b' * 64)
    with pytest.raises(StorageInputError, match='metadata'):
        store(backend, (metadata, replace(asset, producer='different'), path))
    assert not backend.storage_root.exists()


def test_factory_has_no_filesystem_side_effects(tmp_path):
    root = tmp_path / 'missing' / 'nested' / 'storage'
    assert isinstance(get_storage_backend(SimpleNamespace(storage_backend='local', storage_root=root)), LocalStorageBackend)
    assert not root.parent.parent.exists()
    assert not root.exists()
    with pytest.raises(StorageConfigurationError):
        get_storage_backend(SimpleNamespace(storage_backend='drive', storage_root=root))


def test_concurrent_writers_publish_only_one_version(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    inputs = package(tmp_path)
    root = tmp_path / 'storage'
    before = snapshot(inputs[2].parent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store(LocalStorageBackend(root), inputs), range(2)))
    assert sorted(result.created for result in results) == [False, True]
    assert results[0].manifest == results[1].manifest
    assert len(list((root / 'assets' / str(inputs[1].asset_uid)).iterdir())) == 1
    assert not list((root / '.staging').iterdir())
    assert snapshot(inputs[2].parent) == before


def test_existing_empty_version_is_an_integrity_incident(tmp_path):
    inputs = package(tmp_path)
    root = tmp_path / 'storage'
    final = root / 'assets' / str(inputs[1].asset_uid) / observed_package_fingerprint(inputs[1])
    final.mkdir(parents=True)
    with pytest.raises(StorageIntegrityError):
        store(LocalStorageBackend(root), inputs)
    assert list(final.iterdir()) == []
