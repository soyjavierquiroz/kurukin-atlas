"""Synthetic packages and mocked sessions only: never connect to a database."""

import copy
import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.ingest import orchestrator as ingest
from app.ingest.catalog import (
    CatalogPlacement, CatalogResult, logical_asset_values, rendition_values,
    rendition_semantic_overrides, semantics_values,
)
from app.ingest.trust import TrustContractError
from app.ingest.verification import CatalogVerificationError, verify_cataloged_asset
from app.storage import LocalStorageBackend, StorageIntegrityError
from test_local_storage import package, snapshot, uri_path


@pytest.fixture
def source(tmp_path):
    _, _, path = package(tmp_path)
    raw = json.loads(path.read_text())
    raw['analysis']['producer'] = 'movie_broll_extractor'
    raw['editorial'].update(decision='KEEP', action_or_moment_complete='true')
    raw['visual'] = dict(summary_es='Visible scene', visible_emotions=['happy'],
                         relationships=[{'relation': 'near'}],
                         rendition_overrides={'vertical': {'caption': 'original'}})
    raw['narrative'] = {'tone': ['angry']}
    raw['audit_extra'] = {'preserve': ['á', 1]}
    path.write_text(json.dumps(raw), encoding='utf-8')
    return path


def run(path, storage, factory, **kwargs):
    return ingest.ingest_package(path, CatalogPlacement('general'), storage, factory,
                                 stability_seconds=0, sleep=lambda _: None, **kwargs)


@pytest.mark.parametrize('failure', ['missing', 'incomplete', 'trust', 'hash', 'json', 'structure', 'utf8', 'directory', 'escape'])
def test_before_storage(source, failure, tmp_path):
    storage, factory = Mock(), Mock()
    expected = None
    if failure == 'missing':
        source = source.parent / 'absent.json'
    elif failure == 'incomplete':
        (source.parent / 'horizontal space.mp4').unlink()
    elif failure in {'trust', 'structure'}:
        raw = json.loads(source.read_text())
        if failure == 'trust':
            raw['analysis']['semantic_ready'] = False
        else:
            del raw['media']
        source.write_text(json.dumps(raw))
        expected = TrustContractError if failure == 'trust' else ingest.ProducerMetadataError
    elif failure == 'hash':
        (source.parent / 'horizontal space.mp4').write_bytes(b'bad')
        expected = ingest.PackageValidationError
    elif failure in {'json', 'utf8'}:
        source.write_bytes(b'{' if failure == 'json' else b'\xff')
        expected = ingest.ProducerMetadataError
    elif failure == 'directory':
        source = source.parent
        expected = ingest.PackageValidationError
    elif failure == 'escape':
        outside = tmp_path / 'outside.json'
        outside.write_text('{}')
        source.unlink()
        source.symlink_to(outside)
        expected = ingest.PackageValidationError
    before = snapshot(source.parent) if failure != 'directory' else None
    if expected:
        with pytest.raises(expected):
            run(source, storage, factory)
    else:
        result = run(source, storage, factory)
        assert result == ingest.IngestResult(ingest.IngestStatus.NOT_READY)
    assert storage.mock_calls == factory.mock_calls == []
    if before is not None:
        assert snapshot(source.parent) == before


def test_unstable(source):
    storage, factory = Mock(), Mock()
    def arriving(_):
        with source.open('ab') as stream:
            stream.write(b' ')
    result = ingest.ingest_package(source, CatalogPlacement('general'), storage, factory, sleep=arriving)
    assert result.status == ingest.IngestStatus.NOT_READY
    assert storage.mock_calls == factory.mock_calls == []


class Sessions:
    """Tracks transaction exit and guarantees verification uses a distinct session."""
    def __init__(self, events):
        self.events = events
        self.writer = Mock()
        self.reader = Mock()
        self.committed = False

    @contextmanager
    def begin(self):
        self.events.append('begin')
        try:
            yield self.writer
        except Exception:
            self.events.append('rollback')
            raise
        else:
            self.committed = True
            self.events.append('commit')

    @contextmanager
    def __call__(self):
        assert self.committed
        self.events.append('new_session')
        yield self.reader


def catalog_rows(request, state='cataloged'):
    asset = request.normalized_asset
    overrides = rendition_semantic_overrides(asset.rendition_overrides)
    return [
        [SimpleNamespace(asset_uid=asset.asset_uid, **logical_asset_values(asset, request.placement))],
        [SimpleNamespace(asset_uid=asset.asset_uid, kind=kind, **rendition_values(
            getattr(asset, kind), getattr(request.verified_storage_manifest, kind), overrides[kind]))
         for kind in ('horizontal', 'vertical')],
        [SimpleNamespace(asset_uid=asset.asset_uid, **semantics_values(asset))],
        [SimpleNamespace(asset_uid=asset.asset_uid,
                         observed_package_fingerprint=request.observed_package_fingerprint,
                         destination_verified_at=request.destination_verified_at,
                         catalog_committed_at=request.destination_verified_at, state=state)],
    ]


@pytest.fixture
def flow(source, tmp_path, monkeypatch):
    events = []
    factory = Sessions(events)
    backend = LocalStorageBackend(tmp_path / 'storage')
    stored = []
    requests = []
    def store(*args):
        events.append('storage')
        result = backend.store_package(*args)
        stored.append(result)
        return result
    storage = Mock(store_package=Mock(side_effect=store))
    def catalog(session, request):
        assert session is factory.writer
        events.append('catalog')
        requests.append(request)
        factory.reader.scalars.side_effect = catalog_rows(request)
        return CatalogResult(request.normalized_asset.asset_uid, len(requests) == 1,
                             len(requests) == 1, 'cataloged')
    monkeypatch.setattr(ingest, 'catalog_validated_asset', catalog)
    original = ingest.validate_referenced_bytes
    def validate(*args):
        events.append('validate')
        return original(*args)
    monkeypatch.setattr(ingest, 'validate_referenced_bytes', validate)
    return SimpleNamespace(events=events, factory=factory, storage=storage, stored=stored, requests=requests)


def test_success_and_replay(source, flow):
    before = snapshot(source.parent)
    first = run(source, flow.storage, flow.factory)
    assert flow.events == ['validate', 'storage', 'begin', 'catalog', 'commit', 'new_session']
    assert first.status == ingest.IngestStatus.CATALOGED and first.catalog_verified
    assert first.storage_created and first.logical_asset_created and first.package_changed
    assert first.asset_uid == flow.requests[0].normalized_asset.asset_uid
    assert first.package_fingerprint == flow.stored[0].package_fingerprint
    request = flow.requests[0]
    assert request.source_base_uri == source.parent.as_uri()
    assert request.package_basename == source.stem
    assert request.verified_storage_manifest is flow.stored[0].manifest
    assert request.destination_verified_at is flow.stored[0].destination_verified_at
    assert request.normalized_asset.raw_producer_metadata == json.loads(source.read_text())
    replay = run(source, flow.storage, flow.factory)
    assert replay.catalog_verified and replay.ingest_state == 'cataloged'
    assert not replay.storage_created and not replay.logical_asset_created and not replay.package_changed
    assert snapshot(source.parent) == before and len(before) == 5


def test_storage_failure(source, flow):
    before = snapshot(source.parent)
    flow.storage.store_package.side_effect = StorageIntegrityError('injected')
    with pytest.raises(StorageIntegrityError):
        run(source, flow.storage, flow.factory)
    assert flow.events == ['validate']
    assert not flow.factory.committed
    assert snapshot(source.parent) == before


def test_storage_fingerprint_mismatch_stops_before_catalog(source, tmp_path, monkeypatch):
    before = snapshot(source.parent)
    backend = LocalStorageBackend(tmp_path / 'storage')

    def store(*args):
        stored = backend.store_package(*args)
        different_fingerprint = '0' * 64 if args[3] != '0' * 64 else '1' * 64
        return replace(stored, package_fingerprint=different_fingerprint)

    storage = Mock(store_package=Mock(side_effect=store))
    factory = Mock()
    catalog = Mock()
    monkeypatch.setattr(ingest, 'catalog_validated_asset', catalog)

    with pytest.raises(StorageIntegrityError, match='fingerprint'):
        run(source, storage, factory)

    factory.assert_not_called()
    catalog.assert_not_called()
    assert snapshot(source.parent) == before


def test_catalog_failure_keeps_storage_and_retry(source, flow, monkeypatch):
    before = snapshot(source.parent)
    catalog = ingest.catalog_validated_asset
    monkeypatch.setattr(ingest, 'catalog_validated_asset', Mock(side_effect=SQLAlchemyError('injected')))
    with pytest.raises(SQLAlchemyError):
        run(source, flow.storage, flow.factory)
    assert flow.events == ['validate', 'storage', 'begin', 'rollback']
    assert not flow.factory.committed
    flow.factory.reader.scalars.assert_not_called()
    destination = uri_path(flow.stored[0].destination_base_uri)
    preserved = snapshot(destination)
    monkeypatch.setattr(ingest, 'catalog_validated_asset', catalog)
    assert not run(source, flow.storage, flow.factory).storage_created
    assert snapshot(destination) == preserved
    assert snapshot(source.parent) == before


def test_verification_failure_keeps_commit_storage_and_retry(source, flow, monkeypatch):
    before = snapshot(source.parent)
    verifier = ingest.verify_cataloged_asset
    monkeypatch.setattr(ingest, 'verify_cataloged_asset', Mock(side_effect=CatalogVerificationError('injected')))
    with pytest.raises(CatalogVerificationError):
        run(source, flow.storage, flow.factory)
    assert flow.factory.committed and 'rollback' not in flow.events
    destination = uri_path(flow.stored[0].destination_base_uri)
    preserved = snapshot(destination)
    monkeypatch.setattr(ingest, 'verify_cataloged_asset', verifier)
    result = run(source, flow.storage, flow.factory)
    assert result.catalog_verified and not result.storage_created and not result.package_changed
    assert snapshot(destination) == preserved
    assert snapshot(source.parent) == before


@pytest.mark.parametrize('state', ['cataloged', 'cleanup_pending', 'complete'])
def test_verifier_states_and_queries(source, flow, state):
    run(source, flow.storage, flow.factory)
    request = flow.requests[0]
    session = Mock()
    session.scalars.side_effect = catalog_rows(request, state)
    assert verify_cataloged_asset(session, request) == state
    queries = [str(call.args[0]) for call in session.scalars.call_args_list]
    assert len(queries) == 4
    for index in (0, 3):
        assert all(f'{field} =' in queries[index] for field in ('producer', 'source_movie_id', 'producer_asset_id'))
    for index in (1, 2):
        assert 'asset_uid =' in queries[index]
    assert len(session.mock_calls) == 4  # reads only


@pytest.mark.parametrize('table', range(4))
@pytest.mark.parametrize('damage', ['absent', 'duplicate', 'fields'])
def test_verifier_rejects_corruption(source, flow, table, damage):
    run(source, flow.storage, flow.factory)
    request = flow.requests[0]
    rows = catalog_rows(request)
    if damage == 'fields':
        # Every checked field is independently corrupted, including separate semantic evidence.
        fields = [
            ['asset_uid', 'producer', 'source_movie_id', 'producer_asset_id',
             'catalog_scope', 'title_id', 'title_type', 'brand_id', 'status', 'source_movie_sha256'],
            ['asset_uid', 'kind', 'storage_uri', 'thumbnail_uri', 'sha256', 'thumbnail_sha256',
             'size_bytes', 'thumbnail_size_bytes', 'technical_validated', 'semantic_validated', 'semantic_overrides'],
            ['asset_uid', 'raw_producer_metadata', 'visual_summary', 'visible_emotions',
             'narrative_json', 'relationships_json', 'action_or_moment_complete', 'editorial_json'],
            ['asset_uid', 'observed_package_fingerprint', 'destination_verified_at', 'catalog_committed_at', 'state'],
        ][table]
        for field in fields:
            broken = copy.deepcopy(rows)
            setattr(broken[table][0], field, None if field.endswith('_at') else 'corrupt')
            with pytest.raises(CatalogVerificationError):
                verify_cataloged_asset(Mock(scalars=Mock(side_effect=broken)), request)
    else:
        rows[table] = [] if damage == 'absent' else rows[table] + [rows[table][0]]
        with pytest.raises(CatalogVerificationError):
            verify_cataloged_asset(Mock(scalars=Mock(side_effect=rows)), request)
