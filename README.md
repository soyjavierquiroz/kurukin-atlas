# Kurukin Atlas

Kurukin Atlas is Kurukin's curated internal asset retrieval system.

## Responsibilities

- ingest validated producer assets
- maintain logical assets and renditions
- catalog and provenance
- structured and semantic retrieval
- Atlas-local ranking
- contradiction handling
- scarcity
- stable asset resolution for MPT

## Architectural boundary

MBE produces quality.

Atlas stores and finds quality from multiple producers and curated collections.

MPT uses quality.

## P0

Initial dataset: `romper-el-circulo`

Producer contract:

`asset_metadata_v1`

## Core principles

- QUALITY > QUANTITY
- FILE != LOGICAL ASSET
- HORIZONTAL / VERTICAL = RENDITIONS
- VISUAL != NARRATIVE
- QUERY != ASSET EVIDENCE
- UNKNOWN != MISMATCH
- SCARCITY > FAKE MATCH
- SCARCITY != FAILURE
- SCOPE IS HARD
- NO SILENT SCOPE EXPANSION
- ATLAS RANKS ATLAS
- MPT RANKS PROVIDERS
- NO DOUBLE ANALYSIS

## Source and rendition model

Atlas's natural key is `producer + source_key + producer_asset_id`.
`source_key` is opaque and producer-defined; MBE's frozen external
`asset.source_movie_id` maps unchanged to it (for example
`romper-el-circulo`), with `source_kind = movie`. Curated assets use producer
`kurukin_curated`, `source_kind = collection`, and keys such as
`collection:deluxe` and `collection:nature`.

The Atlas core accepts one or more supported renditions. MBE remains a
producer-specific exactly-horizontal-plus-vertical, five-file package contract;
curated assets may be vertical-only. Curated assets initially use the existing
`general` catalog scope: collection identity is provenance, not a catalog scope.
Their editorial or filename/import-list metadata is explicit and provenance
tagged; no AI analysis is required or implied, and unavailable evidence remains
unknown. `ingest_package()` remains the MBE package-ingestion path; a curated
storage/import orchestrator follows Drive work.

The package fingerprint remains v1-compatible for existing MBE custody records:
its canonical JSON intentionally retains the legacy `source_movie_id` label,
but the value is Atlas's `source_key`. This is compatibility serialization, not
Atlas terminology.
