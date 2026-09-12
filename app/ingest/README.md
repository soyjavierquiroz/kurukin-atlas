# Synchronous P0 ingest

`orchestrator.ingest_package(json_path, placement, storage_backend,
session_factory, stability_seconds=1.0, sleep=time.sleep)` processes one package.
Pass an explicit `CatalogPlacement`, `StorageBackend`, and SQLAlchemy
`sessionmaker`. Importing the orchestrator creates no DB or storage service.

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
