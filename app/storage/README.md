# P0 local custody

`LocalStorageBackend(root).store_package(metadata, normalized_asset, json_path,
observed_package_fingerprint)` accepts a previously validated five-file package.
The caller completes readiness, trust, and producer source hash validation first.
Storage checks input identity/member consistency and reuses the shared fingerprint
function in `app.ingest.fingerprint`; it does not interpret producer semantics.

Final layout:

```
<storage_root>/assets/<asset_uid>/<full-package-fingerprint>/<original-filename>
```

All four media files and the original, unmodified JSON are retained. The returned
`VerifiedStoredPackage.manifest` and `destination_verified_at` can be passed to
`CatalogIngestRequest`. `metadata_uri` separately identifies the retained JSON.
All locators are escaped absolute `file://` URIs for internal use.

Each new version is streamed into `<storage_root>/.staging/<unique-id>/`.
Streaming SHA-256 verification checks all four producer hashes, optional producer
sizes, and the source JSON hash/size. Only a complete verified directory is renamed
into place. Staging and final directories must be on the same filesystem; a
cross-device rename fails without publishing. Failures remove only this call's
staging directory. Empty parent directories may remain.

An existing version is verified in full and returned with `created=False` and a
fresh UTC verification timestamp. Missing, extra, corrupt, or nonregular members
are integrity failures and are never repaired or removed. Because the fingerprint
canonicalizes metadata, even a whitespace-only JSON change with the same
fingerprint will fail exact-byte replay verification; it cannot silently replace
the JSON already in custody.

The local backend uses POSIX directory descriptors, `O_NOFOLLOW`, and an advisory
root-directory lock to serialize cooperating Atlas writers across processes.
Destination symlinks are rejected at every level, including root ancestors and
individual files. On `store_package()`, missing `storage_root` components are
created component-by-component using safe fd-relative `O_NOFOLLOW` traversal;
existing symlink components are rejected. The `LocalStorageBackend` constructor
and `get_storage_backend(settings)` factory remain side-effect free, and imports
do not create directories. The root must be Atlas-owned: other processes
must not relocate directories or mutate published versions. Atomic publication
provides visibility, not a power-loss durability guarantee (no fsync protocol).

`get_storage_backend(settings)` supports only `local`; other names raise
`StorageConfigurationError`. No catalog writes or source cleanup occur here.
The next handoff stages remain catalog commit, catalog verification, and only
then a separately implemented exact-source-package cleanup.
