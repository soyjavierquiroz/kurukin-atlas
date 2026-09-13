# Atlas search and asset delivery v1

Atlas exposes only its own frozen catalog. It does not expand scope, invoke a
provider, or fall back to another source. Source cleanup is unrelated to this
API and remains disabled.

`POST /v1/assets/search` accepts the versioned `asset_search_v1` request and
returns `asset_candidates_v1`. A scarcity result is still HTTP 200; inspect
`scarcity_reason` (`no_candidates_in_scope`, `no_requirement_match`,
`no_high_confidence_match`, or `no_acceptable_rendition`).

For example (documentation only; do not run this against production):

```sh
curl -X POST http://localhost:8000/v1/assets/search \
  -H 'content-type: application/json' \
  -d '{"schema_version":"asset_search_v1","query":"conversation","scope":{"kind":"title","title_id":"title-42"}}'
```

Candidates contain logical content and thumbnail locators. Their durable public
identity is `asset_uid` plus `horizontal` or `vertical` rendition kind:

`GET /v1/assets/{asset_uid}/renditions/{kind}/content`

`GET /v1/assets/{asset_uid}/renditions/{kind}/thumbnail`

Content supports one byte range and streams stored media. Storage and Drive
locators are private implementation details: clients never submit or receive
them as API identity.

Read capability is selected from the cataloged locator scheme, not from the
current ingest writer setting. Consequently, a deployment writing new assets
locally can still deliver an allowlisted `rclone://` catalog object when its
rclone read configuration is present, and a deployment writing through rclone
can still deliver a valid local `file://` catalog object. An unsupported or
unconfigured locator scheme is reported as `content_unavailable`.

Failures use `asset_error_v1`, including validation failures. The API returns
ordinary JSON-safe errors only; it does not disclose storage paths, rclone
configuration, Drive identifiers, or operational exceptions. `GET /healthz`
only confirms that the application is alive.

Before delivery headers are sent, Atlas opens the cataloged object and reads at
most one chunk. An unavailable object (including an immediate EOF when the
catalog declares a positive length) is returned as a retryable 503
`content_unavailable` error. A catalog-confirmed zero-length object does not
require a body chunk. After the first body chunk is accepted, headers have
already been emitted: a later storage/process failure closes the underlying
stream and terminates the HTTP body rather than attempting to replace it with a
new JSON error response.
