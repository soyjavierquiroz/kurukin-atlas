import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.contracts.asset_metadata_v1 import AssetMetadataV1
from app.ingest.normalize import atlas_asset_uid, normalize_metadata
from app.ingest.fingerprint import observed_package_fingerprint
from app.ingest.package import (
    PackageReadiness,
    UnsafePackagePathError,
    capture_snapshot,
    package_readiness,
    readiness_from_snapshots,
    safe_package_path,
    validate_referenced_bytes,
)
from app.ingest.trust import TrustContractError, validate_trust

GOLDEN = Path(__file__).parent / "fixtures/mbe/rc162/rc162-pareja-conversando-de-manera-cercana-en-un-entor.json"


def golden_raw():
    return json.loads(GOLDEN.read_text())


def golden_model(raw=None):
    return AssetMetadataV1.model_validate(golden_raw() if raw is None else raw)


def test_golden_mapping_and_action_quirk():
    raw = golden_raw()
    metadata = golden_model(raw)
    assert metadata.schema_version == "asset_metadata_v1"
    assert metadata.analysis.producer == "movie_broll_extractor"
    assert metadata.asset.id == "rc162"
    assert metadata.asset.source_movie_id == "romper-el-circulo"
    assert metadata.editorial.decision == "KEEP"
    assert raw["editorial"]["action_or_moment_complete"] == "true"
    assert metadata.editorial.normalized_action_or_moment_complete is True
    assert (metadata.media.horizontal.width, metadata.media.horizontal.height) == (1920, 800)
    assert (metadata.media.vertical.width, metadata.media.vertical.height) == (600, 800)
    assert metadata.visual.rendition_overrides == {}


@pytest.mark.parametrize("path,value", [
    (("schema_version",), "bad"),
    (("analysis", "producer"), "other"),
    (("editorial", "decision"), "DROP"),
    (("analysis", "semantic_ready"), False),
    (("analysis", "final_asset_semantics_validated"), False),
    (("media", "horizontal", "technical_validated"), False),
    (("media", "horizontal", "semantic_validated"), False),
    (("media", "vertical", "technical_validated"), False),
    (("media", "vertical", "semantic_validated"), False),
    (("source", "movie_sha256"), "not-a-hash"),
])
def test_trust_failures(path, value):
    raw = golden_raw()
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(TrustContractError):
        validate_trust(golden_model(raw))


def test_golden_trust_passes():
    validate_trust(golden_model())


def _synthetic_raw(base: Path, bad_hash_for: str | None = None, size_offset_for: str | None = None):
    contents = {"horizontal": b"horizontal", "vertical": b"vertical", "horizontal_thumbnail": b"h-thumb", "vertical_thumbnail": b"v-thumb"}
    names = {"horizontal": "h.mp4", "vertical": "v.mp4", "horizontal_thumbnail": "h.jpg", "vertical_thumbnail": "v.jpg"}
    for label, content in contents.items():
        (base / names[label]).write_bytes(content)
    raw = golden_raw()
    for kind in ("horizontal", "vertical"):
        item = raw["media"][kind]
        content = contents[kind]
        item["file"] = names[kind]
        item["sha256"] = hashlib.sha256(content).hexdigest()
        item["size_bytes"] = len(content)
        thumb = item["thumbnail"]
        thumb_content = contents[f"{kind}_thumbnail"]
        thumb["file"] = names[f"{kind}_thumbnail"]
        thumb["sha256"] = hashlib.sha256(thumb_content).hexdigest()
        thumb["size_bytes"] = len(thumb_content)
    if bad_hash_for:
        location = raw["media"][bad_hash_for.removesuffix("_thumbnail")]
        target = location["thumbnail"] if bad_hash_for.endswith("_thumbnail") else location
        target["sha256"] = "0" * 64
    if size_offset_for:
        location = raw["media"][size_offset_for.removesuffix("_thumbnail")]
        target = location["thumbnail"] if size_offset_for.endswith("_thumbnail") else location
        target["size_bytes"] += 1
    return raw


def test_package_readiness_missing_stable_and_changed(tmp_path):
    raw = _synthetic_raw(tmp_path)
    metadata = golden_model(raw)
    json_path = tmp_path / "asset.json"
    json_path.write_text("{}")
    (tmp_path / "v.jpg").unlink()
    assert package_readiness(metadata, json_path, sleep=lambda _: None) == PackageReadiness.NOT_READY
    (tmp_path / "v.jpg").write_bytes(b"v-thumb")
    assert package_readiness(metadata, json_path, sleep=lambda _: None) == PackageReadiness.VALID
    members = [json_path, *(tmp_path / name for name in ("h.mp4", "v.mp4", "h.jpg", "v.jpg"))]
    first = capture_snapshot(members)
    (tmp_path / "h.mp4").write_bytes(b"changed")
    assert readiness_from_snapshots(first, capture_snapshot(members)) == PackageReadiness.NOT_READY


@pytest.mark.parametrize("bad_hash,size_offset", [(None, None), ("horizontal", None), ("vertical_thumbnail", None), (None, "horizontal_thumbnail")])
def test_byte_hash_validation(tmp_path, bad_hash, size_offset):
    result = validate_referenced_bytes(golden_model(_synthetic_raw(tmp_path, bad_hash, size_offset)), tmp_path)
    expected = PackageReadiness.VALID if bad_hash is None and size_offset is None else PackageReadiness.INVALID
    assert result.readiness == expected


def test_json_has_no_self_hash_requirement(tmp_path):
    raw = _synthetic_raw(tmp_path)
    (tmp_path / "asset.json").write_text("arbitrary JSON with no hash")
    assert validate_referenced_bytes(golden_model(raw), tmp_path).readiness == PackageReadiness.VALID


def test_safe_package_paths(tmp_path):
    assert safe_package_path(tmp_path, "safe.mp4") == tmp_path / "safe.mp4"
    for unsafe in ("/tmp/no.mp4", "../no.mp4"):
        with pytest.raises(UnsafePackagePathError):
            safe_package_path(tmp_path, unsafe)


def test_safe_package_path_rejects_package_local_symlink_to_outside(tmp_path):
    source_base_uri = tmp_path / "source"
    source_base_uri.mkdir()
    outside_file = tmp_path / "outside.mp4"
    outside_file.write_bytes(b"outside")
    package_local_name = "looks-package-local.mp4"
    symlink = source_base_uri / package_local_name
    try:
        symlink.symlink_to(outside_file)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unsupported: {exc}")

    with pytest.raises(UnsafePackagePathError):
        safe_package_path(source_base_uri, package_local_name)


def test_deterministic_identity_ignores_slug_and_path():
    uid = atlas_asset_uid("movie_broll_extractor", "movie", "asset")
    assert uid == atlas_asset_uid("movie_broll_extractor", "movie", "asset")
    assert uid != atlas_asset_uid("movie_broll_extractor", "movie", "other")
    assert uid != atlas_asset_uid("movie_broll_extractor", "other-movie", "asset")
    raw = golden_raw()
    normalized = normalize_metadata(golden_model(raw), raw)
    assert normalized.source_key == "romper-el-circulo"
    assert normalized.source_kind == "movie"
    assert set(normalized.renditions) == {"horizontal", "vertical"}
    assert normalized.asset_uid == atlas_asset_uid("movie_broll_extractor", "romper-el-circulo", "rc162")
    assert str(normalized.asset_uid) == "0fb1e01e-ee06-5643-89cb-ad9493dd7506"
    assert observed_package_fingerprint(normalized) == "e42e745a994db1a6d633d543b1bcea856c8a0aae66f3acc81371c2a7a7ce864a"
    changed = copy.deepcopy(raw)
    changed["asset"]["slug"] = "new-slug"
    assert normalized.asset_uid == normalize_metadata(golden_model(changed), changed).asset_uid


def test_visual_narrative_separation_and_relationship_provenance():
    raw = golden_raw()
    normalized = normalize_metadata(golden_model(raw), raw)
    assert normalized.visual_summary == raw["visual"]["summary_es"]
    assert normalized.narrative_json["tone"] == ["angry"]
    assert "angry" not in normalized.visible_emotions
    assert normalized.relationships_json[0]["source"] == "srt"
    assert normalized.people_json["overall_presentation"] == "mixed"
    assert normalized.people_json["primary_subject"]["presentation"] == "woman"
