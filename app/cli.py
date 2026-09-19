"""Atlas operator CLI.

The supported executable form is deliberately module based:
``.venv/bin/python -m app.cli``.  No global binary is installed or required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from app.contracts.asset_search_v1 import AssetSearchV1
from app.delivery.reader import StoredObjectReader, get_stored_object_reader
from app.ingest.movie_broll import (
    EpisodeIdentity, ingest_movie_broll_package, preflight_movie_broll,
)
from app.models import AssetRendition, AssetSemantics, IngestRecord, LogicalAsset
from app.search.repository import SqlAlchemySearchRepository
from app.search.service import search_assets


def _identity_from_args(args: argparse.Namespace) -> EpisodeIdentity:
    return EpisodeIdentity(args.series, args.season, args.episode, args.episode_key)


def _item_report(item) -> dict[str, Any]:
    return {
        "package": item.package.json_path.name,
        "producer_asset_id": item.package.producer_asset_id,
        "asset_uid": str(item.asset.asset_uid) if item.asset else None,
        "eligible": item.eligible,
        "existing": item.existing,
        "reconcile": item.reconcile,
        "blocked_reasons": list(item.blocked_reasons),
    }


def _preflight_report(preflight) -> dict[str, Any]:
    return {
        "episode": preflight.identity.provenance,
        "counts": {
            "discovered": preflight.discovered,
            "eligible": preflight.eligible,
            "existing": preflight.existing,
            "new": preflight.new,
            "reconcile": preflight.reconcile,
            "blocked": preflight.blocked,
        },
        "asset_mappings": [_item_report(item) for item in preflight.items],
        "blocked_reasons": [
            {"producer_asset_id": item.package.producer_asset_id, "reasons": list(item.blocked_reasons)}
            for item in preflight.items if item.blocked_reasons
        ],
    }


def ingest_movie_broll_command(args: argparse.Namespace, *,
                               session_factory: sessionmaker[Session] | None = None,
                               storage_backend=None,
                               ffprobe_runner=None,
                               ingest: Callable = ingest_movie_broll_package) -> tuple[int, dict[str, Any]]:
    """Run dry preflight or guarded publication.  This seam is intentionally testable."""

    identity = _identity_from_args(args)
    if session_factory is None:
        from app.db.session import SessionLocal
        session_factory = SessionLocal
    preflight = preflight_movie_broll(args.run, identity, session_factory=session_factory,
                                      ffprobe_runner=ffprobe_runner)
    report = {"command": "ingest movie-broll", **_preflight_report(preflight),
              "created": 0, "reconciled": 0, "unchanged": 0, "verification_results": []}
    report["counts"].update({"created": 0, "reconciled": 0, "unchanged": 0})
    if args.dry_run:
        report["verdict"] = "PASS" if preflight.blocked == 0 else "BLOCKED"
        return (0 if preflight.blocked == 0 else 1), report
    if preflight.blocked:
        report["verdict"] = "BLOCKED"
        return 1, report
    if storage_backend is None:
        from app.settings import get_settings
        from app.storage.rclone_drive import RcloneDriveStorageBackend
        settings = get_settings()
        # API reads may remain on the persistent local backend.  Operator
        # publication explicitly selects the existing Drive writer so the
        # custody path remains KURUKIN_ATLAS/assets/<producer>/<episode_key>/.
        if not settings.rclone_remote or not settings.rclone_root:
            raise ValueError("movie-broll publish requires configured ATLAS_RCLONE_REMOTE and ATLAS_RCLONE_ROOT")
        storage_backend = RcloneDriveStorageBackend(
            binary=settings.rclone_binary, remote=settings.rclone_remote, root=settings.rclone_root,
            lock_path=settings.rclone_lock_path,
        )
    assert session_factory is not None
    for item in preflight.items:
        # blocked items were already rejected above; this call retains the
        # established source->storage->catalog->fresh verification sequence.
        try:
            result = ingest(item.package.json_path, identity, storage_backend, session_factory)
        except Exception as exc:
            report["blocked_reasons"].append({
                "producer_asset_id": item.package.producer_asset_id,
                "reasons": [f"publish failed: {type(exc).__name__}: {exc}"],
            })
            report["counts"]["blocked"] += 1
            report["verdict"] = "FAILED"
            return 1, report
        report["verification_results"].append({
            "producer_asset_id": item.package.producer_asset_id,
            "asset_uid": str(result.asset_uid) if result.asset_uid else None,
            "catalog_verified": result.catalog_verified,
            "ingest_state": result.ingest_state,
        })
        if result.logical_asset_created:
            report["created"] += 1
            report["counts"]["created"] += 1
        elif result.package_changed:
            report["reconciled"] += 1
            report["counts"]["reconciled"] += 1
        else:
            report["unchanged"] += 1
            report["counts"]["unchanged"] += 1
    report["verdict"] = "PASS"
    return 0, report


def _stream_digest(reader: StoredObjectReader, uri: str) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    for chunk in reader.open(uri):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _search_check(session: Session, title_id: str, expected: set[str], kind: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "asset_search_v1", "scope": {"kind": "title", "title_id": title_id},
        "unknown_policy": "allow", "minimum_confidence": "any_eligible", "limit": 100,
    }
    if kind is not None:
        payload["requirements"] = {"rendition": {"acceptable_kinds": [kind], "preferred_kind": kind,
                                                    "preferred_required": True}}
    request = AssetSearchV1.model_validate(payload)
    found = search_assets(SqlAlchemySearchRepository(session), request)
    actual = {candidate.asset_uid for candidate in found.candidates}
    return {"kind": kind or "any", "ok": actual == expected, "expected": len(expected), "found": len(actual)}


def _staging_empty() -> tuple[bool, bool]:
    """Read the active custody staging area without returning a physical path."""

    from app.settings import get_settings
    settings = get_settings()
    if settings.storage_backend == "local":
        staging = Path(settings.storage_root).absolute() / ".staging"
        return True, not staging.exists() or not any(staging.iterdir())
    if settings.storage_backend == "rclone_drive":
        # The Drive publisher's lsjson is the established read-only transport
        # primitive. A transport error is deliberately a failed verification,
        # not an assumption that staging is empty.
        from app.storage.errors import RcloneTransportError
        from app.storage.rclone_drive import RcloneDrivePublisher
        publisher = RcloneDrivePublisher(binary=settings.rclone_binary, remote=settings.rclone_remote or "",
                                         root=settings.rclone_root or "", lock_path=settings.rclone_lock_path)
        try:
            entries = publisher._lsjson(f"{publisher.root}/_staging")
        except RcloneTransportError:
            return False, False
        return True, not entries
    return False, False


def verify_title_command(args: argparse.Namespace, *,
                         session_factory: sessionmaker[Session] | None = None,
                         object_reader: StoredObjectReader | None = None) -> tuple[int, dict[str, Any]]:
    """Read only catalog/search/delivery verification for one title."""

    if session_factory is None:
        from app.db.session import SessionLocal
        session_factory = SessionLocal
    if object_reader is None:
        object_reader = get_stored_object_reader()
    problems: list[str] = []
    samples: list[dict[str, Any]] = []
    with session_factory() as session:
        assets = list(session.scalars(select(LogicalAsset).where(
            LogicalAsset.catalog_scope == "title", LogicalAsset.title_id == args.title_id,
        ).options(selectinload(LogicalAsset.renditions), selectinload(LogicalAsset.semantics),
                  selectinload(LogicalAsset.ingest_record)).order_by(LogicalAsset.asset_uid)))
        producer_ids = [asset.producer_asset_id for asset in assets]
        asset_uids = [str(asset.asset_uid) for asset in assets]
        natural_keys = [(asset.producer, asset.source_key, asset.producer_asset_id) for asset in assets]
        if not assets:
            problems.append("no logical assets for title")
        if len(producer_ids) != len(set(producer_ids)):
            problems.append("duplicate producer IDs")
        if len(asset_uids) != len(set(asset_uids)):
            problems.append("duplicate asset_uids")
        if len(natural_keys) != len(set(natural_keys)):
            problems.append("duplicate natural keys")
        expected = set(asset_uids)
        for kind in ("horizontal", "vertical"):
            check = _search_check(session, args.title_id, expected, kind)
            if not check["ok"]:
                problems.append(f"{kind}-required search did not return title asset set")
        for asset in assets:
            renditions = {rendition.kind: rendition for rendition in asset.renditions}
            if set(renditions) != {"horizontal", "vertical"}:
                problems.append(f"{asset.asset_uid}: missing or duplicate required renditions")
            if asset.semantics is None:
                problems.append(f"{asset.asset_uid}: missing semantics")
            record = asset.ingest_record
            if record is None or record.state not in {"cataloged", "cleanup_pending", "complete"}:
                problems.append(f"{asset.asset_uid}: missing or uncataloged ingest record")
            for kind, rendition in renditions.items():
                if not isinstance(rendition.duration_seconds, (int, float)) or not math.isfinite(rendition.duration_seconds) or rendition.duration_seconds <= 0:
                    problems.append(f"{asset.asset_uid}/{kind}: invalid duration")
                if rendition.thumbnail_uri is None:
                    problems.append(f"{asset.asset_uid}/{kind}: missing thumbnail")
        # first/middle/last deterministic sample, while empty titles remain a
        # successful-but-empty inspection rather than a storage exception.
        indexes = sorted({0, len(assets) // 2, len(assets) - 1}) if assets else []
        for index in indexes:
            asset = assets[index]
            sample = {"asset_uid": str(asset.asset_uid), "members": []}
            for rendition in sorted(asset.renditions, key=lambda value: value.kind):
                for label, uri, expected_hash, expected_size in (
                    ("content", rendition.storage_uri, rendition.sha256, rendition.size_bytes),
                    ("thumbnail", rendition.thumbnail_uri, rendition.thumbnail_sha256, rendition.thumbnail_size_bytes),
                ):
                    ok = False
                    try:
                        if uri is not None and expected_hash is not None:
                            actual_hash, actual_size = _stream_digest(object_reader, uri)
                            ok = actual_hash == expected_hash.lower() and (expected_size is None or actual_size == expected_size)
                    except Exception:
                        ok = False
                    sample["members"].append({"rendition": rendition.kind, "member": label, "ok": ok})
                    if not ok:
                        problems.append(f"{asset.asset_uid}/{rendition.kind}: stored {label} hash or size mismatch")
            samples.append(sample)
        title_search = _search_check(session, args.title_id, expected)
        horizontal_search = _search_check(session, args.title_id, expected, "horizontal")
        vertical_search = _search_check(session, args.title_id, expected, "vertical")
    checked, empty = _staging_empty()
    if not checked:
        problems.append("staging could not be checked")
    elif not empty:
        problems.append("staging is not empty")
    report = {
        "command": "verify title", "title_id": args.title_id,
        "counts": {"logical_assets": len(assets), "producer_ids": len(producer_ids), "asset_uids": len(asset_uids),
                   "natural_keys": len(natural_keys)},
        "verification_results": {
            "horizontal_required_search": horizontal_search,
            "vertical_required_search": vertical_search,
            "title_scoped_search": title_search,
            "staging": {"checked": checked, "empty": empty if checked else None},
            "samples": samples,
        },
        "blocked_reasons": problems,
        "verdict": "PASS" if not problems else "FAILED",
    }
    return (0 if not problems else 1), report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kurukin Atlas operator CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="ingest a producer run")
    ingest_commands = ingest.add_subparsers(dest="ingest_command", required=True)
    movie_broll = ingest_commands.add_parser("movie-broll", help="ingest an MBE episode run")
    movie_broll.add_argument("--run", required=True, help="producer run directory containing assets/")
    movie_broll.add_argument("--series", required=True)
    movie_broll.add_argument("--season", required=True, type=int)
    movie_broll.add_argument("--episode", required=True, type=int)
    movie_broll.add_argument("--episode-key", required=True)
    mode = movie_broll.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--publish", action="store_true")
    movie_broll.add_argument("--report-json")
    verify = commands.add_parser("verify", help="verify cataloged Atlas assets")
    verify_commands = verify.add_subparsers(dest="verify_command", required=True)
    title = verify_commands.add_parser("title", help="verify one title")
    title.add_argument("--title-id", required=True)
    title.add_argument("--report-json")
    return parser


def _emit(report: dict[str, Any], report_json: str | None) -> None:
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    print(payload)
    if report_json:
        # This explicitly requested operator artifact is the only dry-run file
        # output; dry-run itself never changes source, custody, or catalog.
        Path(report_json).write_text(payload + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest" and args.ingest_command == "movie-broll":
            code, report = ingest_movie_broll_command(args)
        else:
            code, report = verify_title_command(args)
    except Exception as exc:
        code, report = 2, {"verdict": "ERROR", "blocked_reasons": [f"{type(exc).__name__}: {exc}"]}
    _emit(report, getattr(args, "report_json", None))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
