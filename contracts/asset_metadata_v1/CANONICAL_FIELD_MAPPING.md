# Atlas consumer mapping for `asset_metadata_v1`

This is an Atlas consumer mapping for the frozen producer V1 document, not a
replacement producer schema. Atlas preserves the complete source JSON and
trusts producer semantics after the trust contract passes. Evidence classes
are deliberately retained: visual evidence is not narrative evidence.

The identity rule is `producer_asset_id == asset.id`. The natural key is:

```
analysis.producer + asset.source_movie_id + asset.id
```

| Atlas concept | V1 JSON path | Type / values | Required | Missing / null behavior | Atlas form | Evidence |
|---|---|---|---|---|---|---|
| schema identity | `schema_version` | string, `asset_metadata_v1` | yes | trust fails | string | identity |
| producer/version | `analysis.producer`, `analysis.producer_version` | string; producer is `movie_broll_extractor` | producer yes, version optional | missing producer trust fails; version `None` | strings | identity |
| semantic trust | `analysis.semantic_ready`, `analysis.final_asset_semantics_validated` | boolean, must true | yes | trust fails | booleans | editorial |
| producer asset/source/slug | `asset.id`, `asset.source_movie_id`, `asset.slug` | strings | id/source yes; slug optional | id/source trust fails; slug `None` | identity fields | identity |
| source checksum | `source.movie_sha256` | 64-char SHA-256 hex | yes | trust fails | string | provenance |
| editorial disposition | `editorial.decision`, `editorial.status` | string; decision must `KEEP` | decision yes; status optional | decision trust fails; status `None` | strings | editorial |
| reusable / standalone | `editorial.reusable_broll`, `editorial.standalone_meaning_es` | boolean / string | optional | `None` | bool/string | editorial |
| complete action/moment | `editorial.action_or_moment_complete` | bool or observed string `"true"`/`"false"` | optional | missing/null/unrecognized -> `None`; raw retained | `bool | None` | editorial |
| discovery terms | `editorial.search_terms_es`, `editorial.negative_use_cases_es` | arrays | optional | empty list | lists | editorial |
| rendition media | `media.horizontal.*`, `media.vertical.*` | file, SHA-256, dimensions, fps, etc. | fields used for validation required; metrics optional | required validation/path/hash fields reject parse/package; metrics `None` | two rendition records | technical |
| rendition trust | `media.{horizontal,vertical}.technical_validated`, `.semantic_validated` | booleans, all must true | yes | trust fails | booleans | technical |
| thumbnails | `media.{horizontal,vertical}.thumbnail.file/.sha256/.size_bytes` | file/SHA required; size optional | file/SHA yes | malformed package; size `None` means no size comparison | thumbnail metadata | technical |
| visual semantics | `visual.summary_es`, `subjects`, `objects`, `actions`, `visible_emotions`, `interaction_labels`, `setting` | string/lists | optional | string `None`, lists empty | separate visual fields | visual |
| people/relationships | `visual.people`, `visual.relationships` | object/list | optional | `{}` / `[]` | separate JSON values, unchanged | visual |
| rendition semantics | `visual.rendition_overrides`, `visual.final_vertical` | object/object | optional | `{}` / `None` | separate JSON values | visual |
| narrative | `narrative` | object or list | optional | `None` | `narrative_json`, never merged into visual fields | narrative |
| timeline | `source_timeline` | object or list | optional | `None` | provenance JSON | provenance |
| auxiliary evidence | `audio`, `export` | object or list | optional | `None` | producer provenance JSON | provenance |

`visual.rendition_overrides` missing or `{}` means that rendition uses the
logical/base visual semantics. `visual.people.overall_presentation` and
`visual.people.primary_subject.presentation` remain separate nested producer
values. Relationships retain their producer provenance (for example,
`source: "srt"`) and are not promoted to visual certainty.

All referenced filenames must be safe package-local filenames. Atlas validates
the bytes of the two videos and two thumbnails, but does not hash the JSON,
source movie, or perform ffprobe/VLM/SRT re-analysis.
