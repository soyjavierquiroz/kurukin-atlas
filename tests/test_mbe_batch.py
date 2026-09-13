"""Read-only MBE outbox discovery and bounded batch tests; no DB or Drive."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.ingest.batch import BatchPackageStatus, ingest_mbe_batch
from app.ingest.catalog import CatalogPlacement
from app.ingest.discovery import DiscoveryStatus, discover_mbe_packages
from app.ingest.orchestrator import IngestResult, IngestStatus


GOLDEN = Path(__file__).parent / "fixtures/mbe/rc162/rc162-pareja-conversando-de-manera-cercana-en-un-entor.json"


def make_package(directory: Path, name: str, *, missing: str | None = None) -> Path:
    """Create a syntactically valid five-file package without trusting hashes."""

    contents = {"h": b"horizontal", "v": b"vertical", "ht": b"h-thumbnail", "vt": b"v-thumbnail"}
    filenames = {"h": f"{name}-h.mp4", "v": f"v{name}-h.mp4",
                 "ht": f"{name}-h.jpg", "vt": f"v{name}-h.jpg"}
    raw = json.loads(GOLDEN.read_text(encoding="utf-8"))
    raw["asset"]["id"] = name
    for kind, video, thumbnail in (("horizontal", "h", "ht"), ("vertical", "v", "vt")):
        rendition = raw["media"][kind]
        rendition["file"] = filenames[video]
        rendition["sha256"] = hashlib.sha256(contents[video]).hexdigest()
        rendition["size_bytes"] = len(contents[video])
        rendition["thumbnail"]["file"] = filenames[thumbnail]
        rendition["thumbnail"]["sha256"] = hashlib.sha256(contents[thumbnail]).hexdigest()
        rendition["thumbnail"]["size_bytes"] = len(contents[thumbnail])
    for label, data in contents.items():
        if label != missing:
            (directory / filenames[label]).write_bytes(data)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def placement(_metadata):
    return CatalogPlacement("title", title_id="romper-el-circulo", title_type="movie")


def source_snapshot(directory: Path) -> dict[str, tuple[int, int, bool]]:
    return {path.name: (path.lstat().st_size, path.lstat().st_mtime_ns, path.is_symlink())
            for path in directory.iterdir()}


def test_complete_package_is_ready_and_provides_identity(tmp_path):
    path = make_package(tmp_path, "complete")
    result = discover_mbe_packages(tmp_path)
    assert result.discovered_json == result.ready == 1
    item = result.packages[0]
    assert item.status == DiscoveryStatus.READY
    assert item.json_path == path
    assert item.package_basename == "complete"
    assert item.producer_asset_id == "complete"


@pytest.mark.parametrize("missing", ["h", "v", "ht", "vt"],
                         ids=["horizontal-video", "vertical-video", "horizontal-thumbnail", "vertical-thumbnail"])
def test_missing_required_member_is_not_ready(tmp_path, missing):
    make_package(tmp_path, "partial", missing=missing)
    assert discover_mbe_packages(tmp_path).packages[0].status == DiscoveryStatus.NOT_READY


def test_malformed_json_unsafe_member_and_duplicate_names_are_invalid(tmp_path):
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    unsafe = make_package(tmp_path, "unsafe")
    raw = json.loads(unsafe.read_text())
    raw["media"]["horizontal"]["file"] = "../outside.mp4"
    unsafe.write_text(json.dumps(raw), encoding="utf-8")
    duplicate = make_package(tmp_path, "duplicate")
    raw = json.loads(duplicate.read_text())
    raw["media"]["vertical"]["file"] = raw["media"]["horizontal"]["file"]
    duplicate.write_text(json.dumps(raw), encoding="utf-8")
    assert [item.status for item in discover_mbe_packages(tmp_path).packages] == [
        DiscoveryStatus.INVALID, DiscoveryStatus.INVALID, DiscoveryStatus.INVALID,
    ]


def test_symlink_member_is_invalid(tmp_path):
    make_package(tmp_path, "symlink")
    member = tmp_path / "symlink-h.mp4"
    target = tmp_path / "target.mp4"
    target.write_bytes(b"other")
    try:
        member.unlink()
        member.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unsupported: {exc}")
    assert discover_mbe_packages(tmp_path).packages[0].status == DiscoveryStatus.INVALID


def test_review_and_nested_directories_are_ignored_and_order_is_lexical(tmp_path):
    make_package(tmp_path, "zeta")
    make_package(tmp_path, "alpha")
    review = tmp_path / "review"
    review.mkdir()
    make_package(review, "review-item")
    nested = tmp_path / "nested"
    nested.mkdir()
    make_package(nested, "nested-item")
    result = discover_mbe_packages(tmp_path)
    assert [item.json_path.name for item in result.packages] == ["alpha.json", "zeta.json"]


def test_discovery_is_non_hashing_and_never_mutates_source(tmp_path, monkeypatch):
    make_package(tmp_path, "safe")
    before = source_snapshot(tmp_path)
    monkeypatch.setattr("app.ingest.package.sha256_file", lambda _: pytest.fail("discovery must not hash media"))
    assert discover_mbe_packages(tmp_path).ready == 1
    assert source_snapshot(tmp_path) == before


def test_dry_run_resolves_selected_work_without_storage_or_ingest(tmp_path):
    make_package(tmp_path, "a")
    storage, factory, ingest = Mock(), Mock(), Mock()
    summary = ingest_mbe_batch(tmp_path, storage, factory, placement, dry_run=True, ingest=ingest)
    assert summary.attempted == summary.cataloged == summary.failed == 0
    assert summary.packages[0].status == BatchPackageStatus.DRY_RUN
    storage.assert_not_called()
    factory.assert_not_called()
    ingest.assert_not_called()


def test_limit_bounds_ready_attempts_not_incomplete_packages(tmp_path):
    make_package(tmp_path, "a")
    make_package(tmp_path, "b", missing="v")
    make_package(tmp_path, "c")
    calls = []

    def ingest(path, *_args):
        calls.append(Path(path).name)
        return IngestResult(IngestStatus.CATALOGED, asset_uid=uuid4())

    summary = ingest_mbe_batch(tmp_path, Mock(), Mock(), placement, limit=1, ingest=ingest)
    assert calls == ["a.json"]
    assert summary.ready == 2 and summary.not_ready == 1
    assert summary.attempted == summary.cataloged == 1
    assert [item.status for item in summary.packages] == [
        BatchPackageStatus.CATALOGED, BatchPackageStatus.NOT_READY, BatchPackageStatus.READY,
    ]


def test_limit_zero_selects_no_ready_packages(tmp_path):
    make_package(tmp_path, "a")
    make_package(tmp_path, "b")
    resolver, ingest = Mock(), Mock()

    summary = ingest_mbe_batch(tmp_path, Mock(), Mock(), resolver, limit=0, ingest=ingest)

    resolver.assert_not_called()
    ingest.assert_not_called()
    assert summary.attempted == summary.cataloged == 0
    assert [item.status for item in summary.packages] == [
        BatchPackageStatus.READY, BatchPackageStatus.READY,
    ]


def test_failure_isolated_and_cataloged_replay_is_accepted(tmp_path):
    make_package(tmp_path, "a")
    make_package(tmp_path, "b")
    calls = []

    def ingest(path, *_args):
        calls.append(Path(path).name)
        if Path(path).stem == "a":
            raise RuntimeError("injected")
        return IngestResult(IngestStatus.CATALOGED, asset_uid=uuid4(), storage_created=False,
                            logical_asset_created=False, package_changed=False, catalog_verified=True)

    summary = ingest_mbe_batch(tmp_path, SimpleNamespace(), Mock(), placement, ingest=ingest)
    assert calls == ["a.json", "b.json"]
    assert summary.attempted == 2 and summary.cataloged == summary.failed == 1
    assert summary.packages[0].error_category == "RuntimeError"
    assert summary.packages[1].status == BatchPackageStatus.CATALOGED


def test_stop_on_error_leaves_later_ready_packages_unattempted(tmp_path):
    for name in ("a", "b", "c"):
        make_package(tmp_path, name)
    resolver = Mock(side_effect=placement)
    ingest = Mock(side_effect=RuntimeError("injected"))

    summary = ingest_mbe_batch(tmp_path, Mock(), Mock(), resolver, stop_on_error=True, ingest=ingest)

    assert [call.args[0].asset.id for call in resolver.call_args_list] == ["a"]
    assert [call.args[0].stem for call in ingest.call_args_list] == ["a"]
    assert summary.attempted == summary.failed == 1
    assert [item.status for item in summary.packages] == [
        BatchPackageStatus.FAILED, BatchPackageStatus.READY, BatchPackageStatus.READY,
    ]


def test_placement_resolver_failure_skips_ingest_and_later_ready_work_continues(tmp_path):
    make_package(tmp_path, "a")
    make_package(tmp_path, "b")
    calls = []

    def resolver(metadata):
        if metadata.asset.id == "a":
            raise RuntimeError("unknown title mapping")
        return placement(metadata)

    def ingest(path, *_args):
        calls.append(Path(path).name)
        return IngestResult(IngestStatus.CATALOGED, asset_uid=uuid4())

    summary = ingest_mbe_batch(tmp_path, Mock(), Mock(), resolver, ingest=ingest)

    assert calls == ["b.json"]
    assert summary.attempted == summary.cataloged == summary.failed == 1
    assert summary.packages[0].status == BatchPackageStatus.FAILED
    assert summary.packages[0].error_category == "PlacementResolverError"
    assert summary.packages[1].status == BatchPackageStatus.CATALOGED


def test_summary_classifications_and_invalid_limit(tmp_path):
    make_package(tmp_path, "ready")
    make_package(tmp_path, "wait", missing="ht")
    (tmp_path / "bad.json").write_text("not json", encoding="utf-8")
    summary = ingest_mbe_batch(tmp_path, Mock(), Mock(), placement, dry_run=True)
    assert (summary.discovered_json, summary.ready, summary.not_ready, summary.invalid) == (3, 1, 1, 1)
    assert [item.status for item in summary.packages] == [
        BatchPackageStatus.INVALID, BatchPackageStatus.DRY_RUN, BatchPackageStatus.NOT_READY,
    ]
    with pytest.raises(ValueError):
        ingest_mbe_batch(tmp_path, Mock(), Mock(), placement, limit=-1)
