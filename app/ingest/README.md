# Synchronous MBE package ingest

`orchestrator.ingest_package(json_path, placement, storage_backend,
session_factory, stability_seconds=1.0, sleep=time.sleep)` processes one package.
Pass an explicit `CatalogPlacement`, `StorageBackend`, and SQLAlchemy
`sessionmaker`. It remains the MBE-only five-file (horizontal plus vertical)
package path, not the future curated importer. Importing the orchestrator creates no DB or storage service.

The sequence is safe UTF-8 JSON read → JSON/Pydantic parse → five-member
readiness/stability → TRUST PRODUCER SEMANTICS → four-member source size/hash
validation → normalization and deterministic UID → shared package fingerprint
→ storage COPY and destination verification → catalog transaction → COMMIT
→ catalog verification through a newly created session → CATALOGED.

Absent JSON, missing package members, and unstable packages return NOT_READY,
without storage or DB access. Invalid JSON/schema raises ProducerMetadataError;
unsafe paths or bad source bytes raise PackageValidationError. Existing trust,
storage, catalog conflict, and SQLAlchemy errors propagate. Raw parsed producer
metadata is retained without rewriting JSON or inferring semantics.

Only the storage backend's VerifiedStoredPackage supplies destination locators
and verification time. Source directory file URI and filename stem are operational
provenance, never rendition locators or logical identity.

Below this MBE boundary, normalized assets, verified storage manifests, catalog
writing, and verification are rendition-generic (one or more supported kinds).
Curated collection assets may be vertical-only and use explicit editorial,
filename, or collection-default provenance without AI analysis.

## Curated single-asset ingest

`curated.ingest_curated_file(CuratedImportSpec(...), curated_storage_backend,
session_factory)` is a separate path for pre-cut collection media. It does not
call `ingest_package()`, which remains the frozen MBE producer-package path.

MBE packages are producer-authored, require horizontal and vertical files, real
thumbnails, and their rich MBE semantic evidence. Curated assets are one or
more explicitly described pre-cut renditions; the initial DELUXE and NATURE
imports are vertical-only and require neither a thumbnail nor AI/VLM analysis.

The importer accepts only an existing, non-empty regular non-symlink local MP4.
It snapshots size and nanosecond mtime before streaming SHA-256 and ffprobe,
then checks the snapshot again. A changed or disappeared source returns
`NOT_READY` before storage is called. ffprobe is an injectable argv-only,
timeout-bounded technical probe: dimensions, duration, frame rate, codec-format
facts, orientation, and aspect ratio are allowed; it never derives people,
objects, action, emotion, luxury, nature, or any other pixel semantics.

Identity is frozen as `kurukin_curated`, `collection`, and
`collection:<collection_id>`, with a caller-provided stable ID (or filename
stem). Thus a DELUXE source defaults to
`(kurukin_curated, collection:deluxe, <stem>)`; NATURE similarly uses
`(kurukin_curated, collection:nature, <stem>)`. Both initially use the single
Atlas `general` catalog placement, never collection-named scopes.

Caller-supplied claims are retained only with one of these provenances:
`editorial`, `filename`, or `collection_defaults`; `none` is valid only with no
semantic claims. UNKNOWN stays UNKNOWN. The optional filename helper merely
strips an extension/prefix and normalizes underscores or hyphens to spaces; it
does no NLP, translation, or inference.

Curated Drive custody contains exactly the original video and Atlas-generated
`atlas-curated.json`. The manifest is a deterministic UTF-8 compact JSON
serialization of `CuratedAssetV1.model_dump(mode="json")` using sorted keys and
`ensure_ascii=False`; its temporary local file is removed after the operation.
No local copy of the video is made. Existing matching immutable revisions are
fully reverified and replay with `created=False`; corrected source bytes or
explicit metadata keep the same natural-key asset UID but produce a new
fingerprint/revision and update the active catalog rendition after verification.

The orchestrator owns `session_factory.begin()`. The catalog writer flushes;
successful context exit commits, and failure rolls back. No pre-catalog state
commits are added. A separate `verify_cataloged_asset(session, expectation)`
checks row cardinalities, identity, placement, custody, producer semantics and
handoff state. It accepts cataloged, cleanup_pending and complete without writes.
The orchestrator calls it only after commit, with a new session. A verification
mismatch raises CatalogVerificationError; success reports the verified state.

Recovery is explicit and caller-driven:

- Failure before storage: no storage or DB call.
- Storage failure: no catalog call; source remains.
- Verified storage + DB failure → KEEP storage → replay. Existing immutable
  storage is reverified with created=False before cataloging again.
- DB commit + verification failure → KEEP DB + storage → replay verification
  through another idempotent ingest. There is no compensating deletion.

**NO SOURCE CLEANUP IN P0 MILESTONE #6**

No cleanup setting is read. Producer files remain after success and failure.
There is no poller, automatic retry loop, CLI, or application service factory.
Tests use synthetic temporary packages, local temporary storage, and mocked
sessions; PostgreSQL behavior remains for manual validation after review.
