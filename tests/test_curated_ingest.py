"""Curated single-file boundary tests: fake probe, storage, and DB sessions only."""

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.ingest.catalog import CatalogResult
from app.ingest.curated import (
    CuratedImportSpec, CuratedProbeError, CuratedProbeTimeoutError,
    CuratedSourceValidationError, canonical_curated_manifest, filename_terms, ingest_curated_file,
    normalize_curated_asset,
    probe_curated_video, streaming_sha256,
)
from app.ingest.fingerprint import observed_package_fingerprint
from app.ingest.orchestrator import IngestStatus
from app.storage.contracts import VerifiedRenditionLocation, VerifiedStorageManifest, VerifiedStoredPackage


class Probe:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": 576, "height": 1024,
                         "avg_frame_rate": "30000/1001"}],
            "format": {"duration": "12.5", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
        }
        self.error = error
        self.calls = []

    def run(self, args, *, timeout_seconds):
        self.calls.append((tuple(args), timeout_seconds))
        if self.error:
            raise self.error
        return json.dumps(self.payload).encode()


class Sessions:
    def __init__(self):
        self.writer = object()
        self.reader = object()
        self.committed = False

    @contextmanager
    def begin(self):
        yield self.writer
        self.committed = True

    @contextmanager
    def __call__(self):
        assert self.committed
        yield self.reader


def stored_package(fingerprint):
    return VerifiedStoredPackage(
        manifest=VerifiedStorageManifest({"vertical": VerifiedRenditionLocation("rclone://final/v.mp4", None)}),
        destination_verified_at=datetime.now(timezone.utc), destination_base_uri="rclone://final",
        metadata_uri="rclone://final/atlas-curated.json", package_fingerprint=fingerprint,
        created=True,
    )


def test_probe_maps_technical_facts_without_semantic_inference(tmp_path):
    probe = Probe()
    technical = probe_curated_video(tmp_path / "v.mp4", runner=probe)
    assert technical == {
        "mime_type": "video/mp4", "duration_seconds": 12.5, "width": 576, "height": 1024,
        "fps": pytest.approx(29.97002997002997), "orientation": "vertical", "aspect_ratio": "576:1024",
    }
    assert probe.calls[0][0][0] == "ffprobe" and "shell" not in " ".join(probe.calls[0][0])
    with pytest.raises(CuratedProbeError):
        probe_curated_video(tmp_path / "v.mp4", runner=Probe(payload={"streams": []}))
    with pytest.raises(CuratedProbeTimeoutError):
        probe_curated_video(tmp_path / "v.mp4", runner=Probe(error=CuratedProbeTimeoutError("timeout")))


def test_canonical_manifest_is_deterministic_utf8_json(tmp_path):
    source = tmp_path / "á.mp4"
    source.write_bytes(b"v")
    # Constructed through the importer path below, the stored metadata is the
    # same object whose model_dump is committed to custody.
    from app.contracts.curated_asset_v1 import CuratedAssetV1
    metadata = CuratedAssetV1.model_validate({
        "schema_version": "curated_asset_v1", "collection_id": "nature", "producer_asset_id": "á",
        "renditions": [{"kind": "vertical", "filename": source.name, "sha256": "a" * 64, "size_bytes": 1}],
        "metadata_source": "editorial", "visual_summary": "Árbol",
    })
    first = canonical_curated_manifest(metadata)
    assert first == canonical_curated_manifest(metadata)
    assert b"\\u00c1" not in first
    assert json.loads(first) == metadata.model_dump(mode="json")


def test_curated_source_rejects_symlink_and_hashes_streaming(tmp_path):
    source = tmp_path / "vertical.mp4"
    source.write_bytes(b"video" * 500_000)
    assert streaming_sha256(source) == (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_size)
    link = tmp_path / "linked.mp4"
    link.symlink_to(source)
    with pytest.raises(CuratedSourceValidationError, match="symlink"):
        ingest_curated_file(CuratedImportSpec(link, "nature"), object(), object(), ffprobe_runner=Probe())


def test_curated_ingest_uses_general_placement_manifest_and_leaves_source_untouched(tmp_path, monkeypatch):
    source = tmp_path / "nature_000758_many_blue_flowers.mp4"
    original = b"single curated source"
    source.write_bytes(original)
    storage_calls, requests = [], []
    stored = []

    class Storage:
        def store_curated_asset(self, metadata, asset, source_path, manifest_path, fingerprint):
            storage_calls.append((metadata, asset, source_path, manifest_path.read_bytes(), fingerprint))
            assert source_path == source and manifest_path.name == "atlas-curated.json"
            assert manifest_path.read_bytes() == canonical_curated_manifest(metadata)
            result = stored_package(fingerprint)
            stored.append(result)
            return result

    import app.ingest.curated as curated_module
    def catalog(session, request):
        requests.append(request)
        return CatalogResult(request.normalized_asset.asset_uid, True, True, "cataloged")
    monkeypatch.setattr(curated_module, "catalog_validated_asset", catalog)
    monkeypatch.setattr(curated_module, "verify_cataloged_asset", lambda session, request: "cataloged")
    result = ingest_curated_file(
        CuratedImportSpec(source, "nature", metadata_source="filename", search_terms=["many blue flowers"]),
        Storage(), Sessions(), ffprobe_runner=Probe(),
    )
    assert result.status == IngestStatus.CATALOGED and result.catalog_verified and result.storage_created
    assert len(storage_calls) == len(requests) == 1
    assert requests[0].placement.catalog_scope == "general"
    assert (requests[0].normalized_asset.producer, requests[0].normalized_asset.source_kind,
            requests[0].normalized_asset.source_key) == (
        "kurukin_curated", "collection", "collection:nature",
    )
    assert set(requests[0].normalized_asset.renditions) == {"vertical"}
    assert requests[0].normalized_asset.renditions["vertical"].thumbnail == {}
    assert requests[0].normalized_asset.subjects == [] and requests[0].normalized_asset.objects == []
    assert source.read_bytes() == original


def test_source_changed_during_probe_returns_not_ready_before_storage(tmp_path):
    source = tmp_path / "deluxe.mp4"
    source.write_bytes(b"before")

    class MutatingProbe(Probe):
        def run(self, args, *, timeout_seconds):
            source.write_bytes(b"after-changed")
            return super().run(args, timeout_seconds=timeout_seconds)

    storage = SimpleNamespace(store_curated_asset=lambda *args: pytest.fail("Drive must not be called"))
    result = ingest_curated_file(CuratedImportSpec(source, "deluxe"), storage, object(), ffprobe_runner=MutatingProbe())
    assert result.status == IngestStatus.NOT_READY


def test_curated_fingerprint_is_independent_of_parent_directory(tmp_path, monkeypatch):
    """A local ingest location is not curated custody metadata."""
    source_a, source_b = tmp_path / "a" / "sample.mp4", tmp_path / "b" / "sample.mp4"
    source_a.parent.mkdir()
    source_b.parent.mkdir()
    source_a.write_bytes(b"same-mp4-bytes")
    source_b.write_bytes(b"same-mp4-bytes")
    requests = []

    class Storage:
        def store_curated_asset(self, metadata, asset, source_path, manifest_path, fingerprint):
            return stored_package(fingerprint)

    import app.ingest.curated as curated_module
    monkeypatch.setattr(curated_module, "catalog_validated_asset", lambda session, request: (
        requests.append(request) or CatalogResult(request.normalized_asset.asset_uid, True, True, "cataloged")
    ))
    monkeypatch.setattr(curated_module, "verify_cataloged_asset", lambda session, request: "cataloged")
    outcomes = [
        ingest_curated_file(
            CuratedImportSpec(source, "nature", producer_asset_id="same-id", metadata_source="editorial",
                              visual_summary="same explicit metadata", search_terms=["same search"]),
            Storage(), Sessions(), ffprobe_runner=Probe(),
        )
        for source in (source_a, source_b)
    ]
    assert [outcome.asset_uid for outcome in outcomes] == [outcomes[0].asset_uid] * 2
    assert [outcome.package_fingerprint for outcome in outcomes] == [outcomes[0].package_fingerprint] * 2
    assert [request.normalized_asset.renditions["vertical"].filename for request in requests] == ["sample.mp4", "sample.mp4"]


def test_curated_same_natural_key_metadata_correction_changes_only_fingerprint():
    from app.contracts.curated_asset_v1 import CuratedAssetV1

    digest = hashlib.sha256(b"same-mp4-bytes").hexdigest()
    common = {
        "schema_version": "curated_asset_v1", "collection_id": "nature", "producer_asset_id": "same-id",
        "renditions": [{"kind": "vertical", "filename": "sample.mp4", "sha256": digest, "size_bytes": 14}],
        "metadata_source": "editorial",
    }
    original = CuratedAssetV1.model_validate({**common, "visual_summary": "first", "search_terms": ["first"]})
    corrected = CuratedAssetV1.model_validate({**common, "visual_summary": "corrected", "search_terms": ["corrected"]})
    original_asset = normalize_curated_asset(original, original.model_dump(mode="json"))
    corrected_asset = normalize_curated_asset(corrected, corrected.model_dump(mode="json"))
    assert original_asset.asset_uid == corrected_asset.asset_uid
    assert observed_package_fingerprint(original_asset) != observed_package_fingerprint(corrected_asset)


def test_curated_same_natural_key_byte_correction_changes_only_fingerprint():
    from app.contracts.curated_asset_v1 import CuratedAssetV1

    common = {
        "schema_version": "curated_asset_v1", "collection_id": "nature", "producer_asset_id": "same-id",
        "metadata_source": "editorial", "visual_summary": "fixed editorial policy",
    }
    first_bytes, corrected_bytes = b"first-mp4", b"fixed-mp4"
    first = CuratedAssetV1.model_validate({**common, "renditions": [{
        "kind": "vertical", "filename": "sample.mp4", "sha256": hashlib.sha256(first_bytes).hexdigest(),
        "size_bytes": len(first_bytes),
    }]})
    corrected = CuratedAssetV1.model_validate({**common, "renditions": [{
        "kind": "vertical", "filename": "sample.mp4", "sha256": hashlib.sha256(corrected_bytes).hexdigest(),
        "size_bytes": len(corrected_bytes),
    }]})
    first_asset = normalize_curated_asset(first, first.model_dump(mode="json"))
    corrected_asset = normalize_curated_asset(corrected, corrected.model_dump(mode="json"))
    assert first_asset.asset_uid == corrected_asset.asset_uid
    assert observed_package_fingerprint(first_asset) != observed_package_fingerprint(corrected_asset)


@pytest.mark.parametrize("failure", ["storage", "catalog", "verification"])
def test_curated_failure_paths_never_mutate_source_or_compensate_storage(tmp_path, monkeypatch, failure):
    source = tmp_path / "nature.mp4"
    source.write_bytes(b"immutable source bytes")
    storage_calls, catalog_calls, verification_calls = [], [], []

    class Storage:
        def __init__(self):
            self.purge_calls = 0
            self.delete_revision_calls = 0

        def purge(self):
            self.purge_calls += 1

        def delete_revision(self):
            self.delete_revision_calls += 1

        def store_curated_asset(self, metadata, asset, source_path, manifest_path, fingerprint):
            storage_calls.append((metadata, fingerprint))
            if failure == "storage":
                raise RuntimeError("storage failed")
            return stored_package(fingerprint)

    import app.ingest.curated as curated_module

    def catalog(session, request):
        catalog_calls.append(request)
        if failure == "catalog":
            raise RuntimeError("catalog failed")
        return CatalogResult(request.normalized_asset.asset_uid, True, True, "cataloged")

    def verify(session, request):
        verification_calls.append(request)
        if failure == "verification":
            raise RuntimeError("verification failed")
        return "cataloged"

    monkeypatch.setattr(curated_module, "catalog_validated_asset", catalog)
    monkeypatch.setattr(curated_module, "verify_cataloged_asset", verify)
    storage = Storage()
    with pytest.raises(RuntimeError, match=failure):
        ingest_curated_file(CuratedImportSpec(source, "nature"), storage, Sessions(), ffprobe_runner=Probe())
    assert source.read_bytes() == b"immutable source bytes"
    if failure == "storage":
        assert catalog_calls == [] and verification_calls == []
    elif failure == "catalog":
        assert len(storage_calls) == 1 and verification_calls == []
    else:
        assert len(storage_calls) == len(catalog_calls) == len(verification_calls) == 1
    assert storage.purge_calls == storage.delete_revision_calls == 0


def test_filename_helper_only_cleans_declared_prefix_and_separators():
    assert filename_terms("nature_000758-many_blue flowers.mp4", remove_prefix="nature_") == (
        "000758 many blue flowers"
    )
    assert filename_terms("nature_000758-many_blue flowers.mp4", remove_prefix="other_") == (
        "nature 000758 many blue flowers"
    )


def test_curated_metadata_source_none_rejects_claims_and_probe_does_not_create_them(tmp_path, monkeypatch):
    with pytest.raises(CuratedSourceValidationError, match="semantic claims"):
        CuratedImportSpec(tmp_path / "v.mp4", "nature", visual_summary="a claim")
    source = tmp_path / "v.mp4"
    source.write_bytes(b"video")
    captured = []

    class Storage:
        def store_curated_asset(self, metadata, asset, source_path, manifest_path, fingerprint):
            captured.append(metadata)
            return stored_package(fingerprint)

    import app.ingest.curated as curated_module
    monkeypatch.setattr(curated_module, "catalog_validated_asset", lambda session, request: CatalogResult(
        request.normalized_asset.asset_uid, True, True, "cataloged"
    ))
    monkeypatch.setattr(curated_module, "verify_cataloged_asset", lambda session, request: "cataloged")
    ingest_curated_file(CuratedImportSpec(source, "nature"), Storage(), Sessions(), ffprobe_runner=Probe())
    assert captured[0].metadata_source == "none"
    assert captured[0].visual_summary is None
    assert captured[0].keywords == captured[0].search_terms == captured[0].negative_use_cases == []
    assert captured[0].standalone_meaning is None
