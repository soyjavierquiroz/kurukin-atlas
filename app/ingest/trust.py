"""Pure metadata trust validation.  It deliberately performs no AI or media analysis."""

from __future__ import annotations

import re

from app.contracts.asset_metadata_v1 import AssetMetadataV1

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class TrustContractError(ValueError):
    """Metadata does not qualify for TRUST PRODUCER SEMANTICS."""


def validate_trust(metadata: AssetMetadataV1) -> None:
    """Raise a specific error unless frozen V1 producer trust conditions all hold."""

    failures: list[str] = []
    if metadata.schema_version != "asset_metadata_v1":
        failures.append("schema_version must be asset_metadata_v1")
    if metadata.analysis.producer != "movie_broll_extractor":
        failures.append("analysis.producer must be movie_broll_extractor")
    if not metadata.asset.id.strip():
        failures.append("asset.id must be non-empty")
    if not metadata.asset.source_movie_id.strip():
        failures.append("asset.source_movie_id must be non-empty")
    if not _SHA256.fullmatch(metadata.source.movie_sha256):
        failures.append("source.movie_sha256 must be SHA-256 hex")
    for kind in ("horizontal", "vertical"):
        rendition = getattr(metadata.media, kind)
        if not rendition.technical_validated:
            failures.append(f"media.{kind}.technical_validated must be true")
        if not rendition.semantic_validated:
            failures.append(f"media.{kind}.semantic_validated must be true")
    if not metadata.analysis.semantic_ready:
        failures.append("analysis.semantic_ready must be true")
    if not metadata.analysis.final_asset_semantics_validated:
        failures.append("analysis.final_asset_semantics_validated must be true")
    # Editorial eligibility is an operator policy layered on top of this
    # producer-trust boundary.  Keep accepts the legacy producer spelling;
    # PASS and REVIEW retain their distinct producer facts for the operator to
    # evaluate (REVIEW must separately carry human approval there).
    if metadata.editorial.decision.strip().upper() not in {"KEEP", "PASS", "REVIEW"}:
        failures.append("editorial.decision must be KEEP, PASS, or REVIEW")
    if failures:
        raise TrustContractError("TRUST PRODUCER SEMANTICS failed: " + "; ".join(failures))
