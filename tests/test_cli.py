"""Operator CLI boundary tests; source packages and DB/storage are synthetic."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.cli import build_parser, ingest_movie_broll_command, verify_title_command
from app.ingest.movie_broll import EpisodeIdentity, preflight_movie_broll
from app.ingest.orchestrator import IngestResult, IngestStatus


def _probe_payload(width=1920, height=800):
    return json.dumps({"streams": [{"codec_type": "video", "width": width, "height": height,
                                     "avg_frame_rate": "24/1"}],
                       "format": {"duration": "5.0", "format_name": "mp4"}}).encode()


class Probe:
    def run(self, args, *, timeout_seconds):
        path = str(args[-1])
        return _probe_payload(600, 800) if "/v-" in path else _probe_payload()


class ReadFactory:
    """A read-only session seam: any natural key is absent."""

    def __call__(self):
        return nullcontext(SimpleNamespace(scalar=lambda statement: None))


def make_package(tmp_path: Path, name: str, *, decision="PASS", approved=False, missing=None) -> Path:
    assets = tmp_path / "run" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    members = {"h": b"horizontal", "v": b"vertical", "ht": b"horizontal-thumb", "vt": b"vertical-thumb"}
    names = {"h": f"h-{name}.mp4", "v": f"v-{name}.mp4", "ht": f"ht-{name}.jpg", "vt": f"vt-{name}.jpg"}
    media = {}
    for kind, video, thumb, width in (("horizontal", "h", "ht", 1920), ("vertical", "v", "vt", 600)):
        media[kind] = {
            "file": names[video], "sha256": hashlib.sha256(members[video]).hexdigest(), "size_bytes": len(members[video]),
            "duration_seconds": 5.0, "width": width, "height": 800, "technical_validated": True,
            "semantic_validated": True,
            "thumbnail": {"file": names[thumb], "sha256": hashlib.sha256(members[thumb]).hexdigest(),
                          "size_bytes": len(members[thumb])},
        }
    editorial = {"decision": decision}
    if approved:
        editorial["human_review_status"] = "APPROVED"
    raw = {"schema_version": "asset_metadata_v1",
           "analysis": {"producer": "movie_broll_extractor", "semantic_ready": True,
                        "final_asset_semantics_validated": True},
           "asset": {"id": name, "source_movie_id": "producer-movie"},
           "source": {"movie_sha256": "a" * 64}, "editorial": editorial,
           "visual": {}, "media": media}
    for label, payload in members.items():
        if label != missing:
            (assets / names[label]).write_bytes(payload)
    path = assets / f"{name}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def identity():
    return EpisodeIdentity("Show", 1, 2, "show-s01e02")


def args(run, *, dry_run=True):
    return SimpleNamespace(run=str(run), series="Show", season=1, episode=2,
                           episode_key="show-s01e02", dry_run=dry_run, publish=not dry_run)


def test_cli_argument_validation_requires_exactly_one_mode():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["ingest", "movie-broll", "--run", "x", "--series", "s", "--season", "1", "--episode", "1", "--episode-key", "k"])
    parsed = parser.parse_args(["ingest", "movie-broll", "--run", "x", "--series", "s", "--season", "1", "--episode", "1", "--episode-key", "k", "--dry-run"])
    assert parsed.dry_run and not parsed.publish


def test_preflight_complete_dry_run_is_read_only_and_maps_episode_identity(tmp_path):
    source = make_package(tmp_path, "one")
    before = {path.name: path.read_bytes() for path in source.parent.iterdir()}
    code, report = ingest_movie_broll_command(args(tmp_path / "run"), session_factory=ReadFactory(), ffprobe_runner=Probe())
    item = report["asset_mappings"][0]
    assert code == 0 and report["counts"] == {"discovered": 1, "eligible": 1, "existing": 0, "new": 1,
                                                "reconcile": 0, "blocked": 0, "created": 0,
                                                "reconciled": 0, "unchanged": 0}
    assert item["eligible"] and item["asset_uid"]
    assert {path.name: path.read_bytes() for path in source.parent.iterdir()} == before
    preflight = preflight_movie_broll(tmp_path / "run", identity(), session_factory=ReadFactory(), ffprobe_runner=Probe())
    asset = preflight.items[0].asset
    assert asset
    # The raw producer document remains untouched; the Atlas natural key is the episode.
    assert asset.raw_producer_metadata["asset"]["source_movie_id"] == "producer-movie"
    assert asset.source_key == identity().episode_key
    assert asset.producer_provenance["atlas_episode"] == identity().provenance


@pytest.mark.parametrize("missing, duplicate, decision, approved, expected", [
    ("v", False, "PASS", False, False),
    (None, True, "PASS", False, False),
    (None, False, "REVIEW", False, False),
    (None, False, "REVIEW", True, True),
    (None, False, "PASS", False, True),
])
def test_preflight_blocks_incomplete_duplicates_and_unapproved_review(tmp_path, missing, duplicate, decision, approved, expected):
    make_package(tmp_path, "one", decision=decision, approved=approved, missing=missing)
    if duplicate:
        second = make_package(tmp_path, "two", decision=decision, approved=approved)
        raw = json.loads(second.read_text())
        raw["asset"]["id"] = "one"
        second.write_text(json.dumps(raw))
    result = preflight_movie_broll(tmp_path / "run", identity(), session_factory=ReadFactory(), ffprobe_runner=Probe())
    assert (result.blocked == 0) is expected
    if expected:
        assert all(item.eligible for item in result.items)


def test_episode_uid_is_stable_and_publish_rerun_reports_unchanged(tmp_path):
    make_package(tmp_path, "one")
    first = preflight_movie_broll(tmp_path / "run", identity(), session_factory=ReadFactory(), ffprobe_runner=Probe())
    second = preflight_movie_broll(tmp_path / "run", identity(), session_factory=ReadFactory(), ffprobe_runner=Probe())
    assert first.items[0].asset.asset_uid == second.items[0].asset.asset_uid
    outcomes = iter((IngestResult(IngestStatus.CATALOGED, first.items[0].asset.asset_uid,
                                  logical_asset_created=True, package_changed=True, catalog_verified=True),
                     IngestResult(IngestStatus.CATALOGED, first.items[0].asset.asset_uid,
                                  logical_asset_created=False, package_changed=False, catalog_verified=True)))
    call = lambda *unused: next(outcomes)
    code_a, report_a = ingest_movie_broll_command(args(tmp_path / "run", dry_run=False), session_factory=ReadFactory(),
                                                   storage_backend=object(), ffprobe_runner=Probe(), ingest=call)
    code_b, report_b = ingest_movie_broll_command(args(tmp_path / "run", dry_run=False), session_factory=ReadFactory(),
                                                   storage_backend=object(), ffprobe_runner=Probe(), ingest=call)
    assert code_a == code_b == 0 and report_a["created"] == 1 and report_b["unchanged"] == 1


def test_publish_failure_preserves_source_and_reports_nonzero(tmp_path):
    source = make_package(tmp_path, "one")
    before = {path.name: path.read_bytes() for path in source.parent.iterdir()}
    def fail(*unused):
        raise RuntimeError("publisher failed")
    code, report = ingest_movie_broll_command(args(tmp_path / "run", dry_run=False), session_factory=ReadFactory(),
                                               storage_backend=object(), ffprobe_runner=Probe(), ingest=fail)
    assert code == 1 and report["verdict"] == "FAILED"
    assert {path.name: path.read_bytes() for path in source.parent.iterdir()} == before


def test_verify_title_empty_is_read_only_but_fails_verification(monkeypatch):
    class Rows(list):
        def all(self):
            return list(self)
    class SessionFactory:
        def __call__(self):
            return nullcontext(SimpleNamespace(scalars=lambda statement: Rows()))
    monkeypatch.setattr("app.cli._staging_empty", lambda: (True, True))
    code, report = verify_title_command(SimpleNamespace(title_id="show-s01e02"), session_factory=SessionFactory(), object_reader=object())
    assert code == 1 and report["verdict"] == "FAILED" and "no logical assets for title" in report["blocked_reasons"]
