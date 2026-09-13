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

`get_storage_backend(settings)` supports `local` (the unchanged active default)
and inactive-on-config `rclone_drive`. No catalog writes or source cleanup occur
here. The next handoff stages remain catalog commit, catalog verification, and
only then a separately implemented exact-source-package cleanup.

## P0 rclone Google Drive custody

`RcloneDrivePublisher` is the shared low-level publisher for explicit remote
members. It owns identity-derived Drive paths, the writer lock, UUID staging,
`copyto`, exact staging/final verification, absence recheck, `moveto`, SHA-256
fallback via streamed `cat`, immutable replay, URI escaping, and staging-only
purge safety. `RcloneDriveStorageBackend` is the MBE five-member adapter;
`RcloneDriveCuratedStorageBackend` is its curated counterpart, so neither path
duplicates the promotion algorithm.

`RcloneDriveStorageBackend` uses explicit rclone CLI argument arrays (`mkdir`,
`lsjson`, `copyto`, `moveto`, `cat`, and narrowly scoped best-effort `purge`); it does not
mount Drive, inspect rclone configuration, or use a shell. Construction has no
remote or filesystem side effects. Its command runner is injectable, so tests
never access rclone or Google Drive.

The frozen final layout is:

```
<root>/assets/<encoded-producer>/<encoded-source_key>/
  <encoded-producer_asset_id>--<full-package-fingerprint>/<original-member>
```

For rc162 this is conceptually
`Javier/KURUKIN_ATLAS/assets/movie_broll_extractor/romper-el-circulo/rc162--<full-sha256>/`.
Identity components use reversible percent encoding (for example,
`collection:deluxe` becomes `collection%3Adeluxe`); Drive paths and Drive IDs
are custody details, not Atlas identity.

Publication is:

```
source -> unique remote _staging UUID -> remote hash verification
       -> server-side directory moveto -> final remote hash verification
       -> VerifiedStoredPackage -> catalog
```

Only the exact five MBE members are hashed locally and uploaded individually
with `copyto`; no local media staging copy is made. `lsjson --hash` establishes
the exact remote member set, size, and SHA-256. If an otherwise ordinary object
does not expose SHA-256, the backend uses streamed `rclone cat` hashing; it
never accepts size alone.

An exact existing final directory is reverified and returned with
`created=False`. A missing, extra, malformed, wrong-size, or wrong-hash final
is an immutable integrity incident: it is never merged, repaired, overwritten,
or removed. Failures before promotion may best-effort purge only that call's
UUID staging directory. If `moveto` succeeded but final verification fails, the
final is intentionally preserved as evidence and is not purged.

P0 uses a local `flock` lock file created only by a publication call (by default
`/opt/apps/kurukin-atlas/data/locks/rclone-drive.lock`). This assumes
all Drive writers run through this Atlas host (or otherwise cooperate on this
same lock); it is not distributed locking. Returned internal URIs use
`rclone://<remote-without-colon>/<remote-relative-path>` and always refer to
the final directory or final members, never staging paths or Drive IDs. URI
serialization escapes physical percent signs too, so a single normal URI decode
recovers the exact rclone path.

For curated vertical-only custody, the generic publisher receives exactly two
`RemoteMember` records: the original MP4 and `atlas-curated.json`. The returned
manifest has `renditions == {"vertical"}`, a final video URI, and
`thumbnail_uri=None`; there is no fabricated horizontal rendition or thumbnail.
For example, a DELUXE revision is physically stored below
`assets/kurukin_curated/collection%3Adeluxe/<asset-id>--<fingerprint>/`.
The `rclone://` URI serializes that physical `%3A` as `%253A`, so one URI decode
recovers the exact remote-relative name.
