"""Stable package identity shared by custody and catalog ingestion."""

import hashlib
import json

from app.ingest.normalize import NormalizedAsset


def observed_package_fingerprint(asset: NormalizedAsset) -> str:
    """Return a stable SHA-256 fingerprint of meaningful validated package content."""

    canonical = {
        "natural_key": {
            "producer": asset.producer,
            # This legacy label is intentionally frozen for fingerprint-v1 byte
            # compatibility; Atlas's actual natural-key field is source_key.
            "source_movie_id": asset.source_key,
            "producer_asset_id": asset.producer_asset_id,
        },
        "source_movie_sha256": asset.source_movie_sha256,
        "renditions": {
            kind: {"sha256": rendition.sha256, "thumbnail_sha256": rendition.thumbnail.get("sha256")}
            for kind, rendition in asset.renditions.items()
        },
        "raw_producer_metadata": asset.raw_producer_metadata,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
