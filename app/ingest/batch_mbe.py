"""Small explicit CLI for the current MBE title outbox; no daemon or cleanup."""

from __future__ import annotations

import argparse
import json

from app.ingest.batch import ingest_mbe_batch
from app.ingest.catalog import CatalogPlacement
from app.settings import get_settings
from app.storage.backend import get_storage_backend


def _placement_for_current_title(metadata, source_movie_id: str, title_id: str) -> CatalogPlacement:
    if metadata.asset.source_movie_id != source_movie_id:
        raise ValueError(f"unexpected source_movie_id: {metadata.asset.source_movie_id!r}")
    return CatalogPlacement("title", title_id=title_id, title_type="movie")


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover and sequentially ingest MBE outbox packages.")
    parser.add_argument("--outbox", help="MBE asset directory (defaults to ATLAS_MBE_OUTBOX)")
    parser.add_argument("--limit", type=int, help="maximum READY packages to attempt")
    parser.add_argument("--dry-run", action="store_true", help="discover and resolve only; never ingest")
    parser.add_argument("--source-movie-id", required=True,
                        help="expected producer source_movie_id for this explicit title mapping")
    parser.add_argument("--title-id", required=True, help="Atlas title placement identifier")
    args = parser.parse_args()
    settings = get_settings()
    # A dry run does not even construct a storage backend or import the DB
    # session factory. A real run takes its backend exclusively from settings;
    # local therefore stays local and is never silently switched to Drive.
    storage_backend = None
    session_factory = None
    if not args.dry_run:
        from app.db.session import SessionLocal
        storage_backend = get_storage_backend(settings)
        session_factory = SessionLocal
    summary = ingest_mbe_batch(
        args.outbox or settings.mbe_outbox,
        storage_backend,
        session_factory,
        lambda metadata: _placement_for_current_title(metadata, args.source_movie_id, args.title_id),
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(json.dumps({
        "discovered_json": summary.discovered_json,
        "ready": summary.ready,
        "not_ready": summary.not_ready,
        "invalid": summary.invalid,
        "attempted": summary.attempted,
        "cataloged": summary.cataloged,
        "failed": summary.failed,
        "ingest_not_ready": summary.ingest_not_ready,
        "packages": [
            {"json_path": str(item.package.json_path), "package_basename": item.package.package_basename,
             "producer_asset_id": item.package.producer_asset_id, "status": item.status,
             "asset_uid": str(item.asset_uid) if item.asset_uid else None,
             "error_category": item.error_category, "error_message": item.error_message}
            for item in summary.packages
        ],
    }, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
