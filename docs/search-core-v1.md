# Atlas Search Core v1

Atlas search is provider-local application logic. MBE produces quality; Atlas
custodies and finds that quality; MPT will use it. This core has no HTTP
endpoint, provider orchestration, external asset source, vector search, or
embeddings.

`asset_search_v1` is the request contract and `asset_candidates_v1` is the
response contract. Query text expresses retrieval intent only: it never becomes
asset evidence. Requirements are hard eligibility tests; preferences are soft
ranking signals and never exclude an otherwise eligible candidate.

Each requested hard dimension reports `MATCH`, `UNKNOWN`, or `MISMATCH` from
stored producer/editorial evidence. Open semantic fields are `MATCH` only when
positive evidence supports the claim and `MISMATCH` only when applicable
explicit negative evidence contradicts it; absence or non-support is
`UNKNOWN`. Known numeric people counts are closed evidence, so an out-of-range
count is `MISMATCH`. The conservative default excludes `UNKNOWN`; callers may
explicitly allow it, where it remains visibly uncertain. No absent catalog
field is inferred. Apparent
people presentation is only stored producer/editorial visual metadata, never
identity, biological sex, or gender-identity inference.

Scope is a hard database filter: `title`, `titles`, `all_titles`, `brand`, and
`general` return only their stated placement; `catalog` spans title, brand, and
general. Scope is never silently expanded.

The selected rendition must be acceptable and present. A preferred rendition is
soft unless explicitly required. Matching uses logical semantics plus explicit
selected-rendition overrides; an override wins only for fields it provides.
Durable consumer identity is `asset_uid + selected rendition`; response
locators are logical content/thumbnail endpoint concepts, not storage identity.
Source provenance includes the producer's asset ID only for traceability; it is
not a replacement durable identity.

The SQL adapter applies scope first, eagerly loads semantics and renditions, and
fetches at most its internal deterministic maximum pool plus one asset. An
over-capacity scoped result raises `SearchPoolCapacityError`; it is never
silently truncated into an authoritative response. Custom repositories share
this contract. The pure matcher then
ranks lexical/editorial evidence (visual summary, search terms, keywords,
subjects, objects, actions, setting, emotions, relationships, and standalone
meaning) above technical metadata. Ties sort by `asset_uid`. Atlas scores only
Atlas candidates.

Scarcity is a valid result, never padding: `no_candidates_in_scope`,
`no_requirement_match`, `no_high_confidence_match`, and
`no_acceptable_rendition`. The public `limit` is only a maximum.
