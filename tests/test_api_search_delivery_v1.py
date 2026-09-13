"""FastAPI boundary tests with only fakes: no database, Drive, or network."""

from collections.abc import Iterator
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from app.api.dependencies import get_delivery_repository, get_search_repository
from app.api.main import _open_stream, app
from app.contracts.asset_search_v1 import AssetSearchV1
from app.delivery.reader import StoredObjectReadError, get_stored_object_reader
from app.delivery.repository import DeliveryAsset, DeliveryRendition
from app.search.errors import SearchPoolCapacityError
from app.search.service import SearchCandidateEvidence, SearchRenditionEvidence


ASSET_UID = uuid4()


class SearchRepository:
    def __init__(self, rows=(), error: Exception | None = None):
        self.rows = rows
        self.error = error

    def fetch_candidates(self, scope, *, pool_limit):
        if self.error:
            raise self.error
        return self.rows


class DeliveryRepository:
    def __init__(self, asset: DeliveryAsset | None):
        self.asset = asset

    def get_asset(self, asset_uid: UUID):
        return self.asset if self.asset and asset_uid == self.asset.asset_uid else None


class BytesReader:
    def __init__(self, data=b"video-bytes", error=False):
        self.data = data
        self.error = error
        self.calls = []

    def open(self, uri: str, *, offset=0, count=None) -> Iterator[bytes]:
        self.calls.append((uri, offset, count))
        if self.error:
            raise StoredObjectReadError("private locator")
        return iter((self.data[offset:] if count is None else self.data[offset:offset + count],))


def rendition(*, thumbnail=True):
    return DeliveryRendition(
        kind="horizontal", storage_uri="file:///private/video.mp4", thumbnail_uri="file:///private/thumb.jpg" if thumbnail else None,
        sha256="a" * 64, size_bytes=11, mime_type="video/mp4",
        thumbnail_sha256="b" * 64 if thumbnail else None, thumbnail_size_bytes=10 if thumbnail else None,
    )


def asset(*, thumbnail=True, status="active", renditions=None):
    return DeliveryAsset(ASSET_UID, status, renditions if renditions is not None else {"horizontal": rendition(thumbnail=thumbnail)})


@pytest.fixture(autouse=True)
def clean_overrides():
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


def client(search=SearchRepository(), delivery=None, reader=BytesReader()):
    app.dependency_overrides[get_search_repository] = lambda: search
    app.dependency_overrides[get_delivery_repository] = lambda: DeliveryRepository(delivery)
    app.dependency_overrides[get_stored_object_reader] = lambda: reader
    return TestClient(app)


def search_body():
    return {"schema_version": "asset_search_v1", "query": "conversation", "scope": {"kind": "title", "title_id": "t-1"}}


def test_health_and_search_contracts_preserve_hard_scope_and_scarcity():
    row = SearchCandidateEvidence(
        asset_uid=str(ASSET_UID), producer="mbe", source_kind="movie", source_key="s", producer_asset_id="p",
        catalog_scope="title", title_id="t-1", visual_summary="conversation",
        renditions={"horizontal": SearchRenditionEvidence("horizontal", True)},
    )
    with client(search=SearchRepository((row,))) as http:
        assert http.get("/healthz").json() == {"status": "ok"}
        found = http.post("/v1/assets/search", json=search_body())
        assert found.status_code == 200
        assert found.json()["schema_version"] == "asset_candidates_v1"
        locator = found.json()["candidates"][0]["selected_rendition"]["content_locator"]
        assert locator == f"/v1/assets/{ASSET_UID}/renditions/horizontal/content"
        assert "file:" not in found.text and "rclone:" not in found.text
    with client() as http:
        scarce = http.post("/v1/assets/search", json=search_body())
        assert scarce.status_code == 200
        assert scarce.json()["scarcity_reason"] == "no_candidates_in_scope"


def test_search_capacity_and_validation_use_sanitized_error_contract():
    with client(search=SearchRepository(error=SearchPoolCapacityError("internal bound"))) as http:
        response = http.post("/v1/assets/search", json=search_body())
        assert (response.status_code, response.json()["code"], response.json()["retryable"]) == (503, "search_capacity_exceeded", True)
        malformed = http.post("/v1/assets/search", json={"scope": {"kind": "title"}})
        assert malformed.status_code == 422
        assert malformed.json() == {"schema_version": "asset_error_v1", "code": "invalid_request", "message": "Request validation failed.", "retryable": False, "details": None}
        forbidden_locator = {**search_body(), "storage_uri": "rclone://private/object"}
        assert http.post("/v1/assets/search", json=forbidden_locator).json()["code"] == "invalid_request"


def test_openapi_uses_the_versioned_pydantic_contracts():
    schemas = app.openapi()["components"]["schemas"]
    assert schemas["AssetSearchV1"]["title"] == "asset_search_v1"
    assert schemas["AssetCandidatesV1"]["title"] == "asset_candidates_v1"
    assert schemas["AssetErrorV1"]["title"] == "asset_error_v1"


def test_delivery_identity_errors_and_stream_headers():
    reader = BytesReader(b"hello world")
    with client(delivery=asset(), reader=reader) as http:
        unknown = http.get(f"/v1/assets/{uuid4()}/renditions/horizontal/content")
        assert (unknown.status_code, unknown.json()["code"]) == (404, "asset_not_found")
        missing = http.get(f"/v1/assets/{ASSET_UID}/renditions/vertical/content")
        assert (missing.status_code, missing.json()["code"]) == (404, "rendition_not_found")
        malformed = http.get("/v1/assets/not-a-uuid/renditions/horizontal/content")
        assert (malformed.status_code, malformed.json()["code"]) == (422, "invalid_request")
        response = http.get(f"/v1/assets/{ASSET_UID}/renditions/horizontal/content")
        assert response.content == b"hello world"
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["content-length"] == "11"
        assert response.headers["etag"] == '"' + "a" * 64 + '"'
        assert response.headers["accept-ranges"] == "bytes"
        thumbnail = http.get(f"/v1/assets/{ASSET_UID}/renditions/horizontal/thumbnail")
        assert thumbnail.content == b"hello world"
        assert thumbnail.headers["content-type"] == "image/jpeg"
    with client(delivery=asset(thumbnail=False)) as http:
        absent = http.get(f"/v1/assets/{ASSET_UID}/renditions/horizontal/thumbnail")
        assert (absent.status_code, absent.json()["code"]) == (404, "thumbnail_not_available")


@pytest.mark.parametrize(
    ("range_value", "status", "body", "content_range"),
    [("bytes=1-3", 206, b"ell", "bytes 1-3/11"), ("bytes=6-", 206, b"world", "bytes 6-10/11"), ("bytes=-5", 206, b"world", "bytes 6-10/11"), ("bytes=20-21", 416, None, "bytes */11")],
)
def test_single_byte_ranges(range_value, status, body, content_range):
    with client(delivery=asset(), reader=BytesReader(b"hello world")) as http:
        response = http.get(f"/v1/assets/{ASSET_UID}/renditions/horizontal/content", headers={"Range": range_value})
        assert response.status_code == status
        assert response.headers["content-range"] == content_range
        if body is not None:
            assert response.content == body
            assert response.headers["content-length"] == str(len(body))


def test_reader_failure_and_unexpected_failure_are_sanitized_before_streaming():
    with client(delivery=asset(), reader=BytesReader(error=True)) as http:
        unavailable = http.get(f"/v1/assets/{ASSET_UID}/renditions/horizontal/content")
        assert (unavailable.status_code, unavailable.json()["code"], unavailable.json()["retryable"]) == (503, "content_unavailable", True)

    class BrokenDelivery:
        def get_asset(self, asset_uid):
            raise RuntimeError("rclone://secret stderr")

    app.dependency_overrides[get_delivery_repository] = lambda: BrokenDelivery()
    app.dependency_overrides[get_stored_object_reader] = lambda: BytesReader()
    with TestClient(app, raise_server_exceptions=False) as http:
        unexpected = http.get(f"/v1/assets/{ASSET_UID}/renditions/horizontal/content")
        assert (unexpected.status_code, unexpected.json()["code"]) == (500, "internal_error")
        assert "secret" not in unexpected.text


def test_first_iteration_failures_are_preflighted_and_closed_for_content_range_and_thumbnail():
    class FirstIterationFailureReader:
        def __init__(self, error):
            self.error = error
            self.closed = 0
            self.calls = []

        def open(self, uri, *, offset=0, count=None):
            self.calls.append((uri, offset, count))

            def stream():
                try:
                    raise self.error
                    yield b"unreachable"
                finally:
                    self.closed += 1

            return stream()

    unavailable_reader = FirstIterationFailureReader(StoredObjectReadError("rclone://private stderr"))
    with client(delivery=asset(), reader=unavailable_reader) as http:
        content = http.get(f"/v1/assets/{ASSET_UID}/renditions/horizontal/content")
        ranged = http.get(f"/v1/assets/{ASSET_UID}/renditions/horizontal/content", headers={"Range": "bytes=1-3"})
        thumb = http.get(f"/v1/assets/{ASSET_UID}/renditions/horizontal/thumbnail")
        assert (content.status_code, content.json()["code"]) == (503, "content_unavailable")
        assert (ranged.status_code, ranged.json()["code"]) == (503, "content_unavailable")
        assert (thumb.status_code, thumb.json()["code"]) == (503, "content_unavailable")
    assert unavailable_reader.calls[1][1:] == (1, 3)
    assert unavailable_reader.closed == 3

def test_unexpected_first_iteration_failure_is_sanitized_and_closed():
    class BrokenReader:
        def __init__(self):
            self.closed = False

        def open(self, uri, *, offset=0, count=None):
            def stream():
                try:
                    raise RuntimeError("rclone://private stderr")
                    yield b"unreachable"
                finally:
                    self.closed = True

            return stream()

    reader = BrokenReader()
    app.dependency_overrides[get_search_repository] = lambda: SearchRepository()
    app.dependency_overrides[get_delivery_repository] = lambda: DeliveryRepository(asset())
    app.dependency_overrides[get_stored_object_reader] = lambda: reader
    with TestClient(app, raise_server_exceptions=False) as http:
        response = http.get(f"/v1/assets/{ASSET_UID}/renditions/horizontal/content")
    assert (response.status_code, response.json()["code"]) == (500, "internal_error")
    assert "private" not in response.text
    assert reader.closed


def test_preflight_keeps_exactly_one_chunk_and_closes_on_early_close_and_late_failure():
    events = []

    def source():
        try:
            events.append("first")
            yield b"one"
            events.append("second")
            yield b"two"
        finally:
            events.append("closed")

    class Reader:
        def open(self, uri, *, offset=0, count=None):
            return source()

    # A client can disconnect after preflight but before Starlette asks for the
    # first body chunk. The wrapper still owns and closes the started source.
    before_first = _open_stream(Reader(), "file:///ignored", expected_size=6)
    assert events == ["first"]
    before_first.close()
    assert events == ["first", "closed"]

    events.clear()
    body = _open_stream(Reader(), "file:///ignored", expected_size=6)
    assert events == ["first"]
    assert next(body) == b"one"
    assert events == ["first"]
    body.close()
    assert events == ["first", "closed"]

    closed = []

    def failing_source():
        try:
            yield b"one"
            raise StoredObjectReadError("storage details must not become a JSON response")
        finally:
            closed.append(True)

    class FailingReader:
        def open(self, uri, *, offset=0, count=None):
            return failing_source()

    late = _open_stream(FailingReader(), "file:///ignored", expected_size=6)
    assert next(late) == b"one"
    with pytest.raises(StoredObjectReadError):
        next(late)
    assert closed == [True]


def test_immediate_eof_with_positive_catalog_length_is_unavailable_before_headers():
    class EmptyReader:
        def open(self, uri, *, offset=0, count=None):
            return iter(())

    with client(delivery=asset(), reader=EmptyReader()) as http:
        content = http.get(f"/v1/assets/{ASSET_UID}/renditions/horizontal/content")
        thumbnail = http.get(f"/v1/assets/{ASSET_UID}/renditions/horizontal/thumbnail")
        assert (content.status_code, content.json()["code"]) == (503, "content_unavailable")
        assert (thumbnail.status_code, thumbnail.json()["code"]) == (503, "content_unavailable")


def test_zero_length_catalog_content_does_not_require_a_first_chunk():
    class ReaderThatMustNotIterate:
        def __init__(self):
            self.opened = 0

        def open(self, uri, *, offset=0, count=None):
            self.opened += 1

            def stream():
                raise AssertionError("zero-length delivery must not read a chunk")
                yield b"unreachable"

            return stream()

    empty = DeliveryRendition(
        kind="horizontal",
        storage_uri="file:///private/empty.mp4",
        thumbnail_uri=None,
        sha256="a" * 64,
        size_bytes=0,
        mime_type="video/mp4",
    )
    reader = ReaderThatMustNotIterate()
    with client(delivery=asset(renditions={"horizontal": empty}), reader=reader) as http:
        response = http.get(f"/v1/assets/{ASSET_UID}/renditions/horizontal/content")
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == "0"
    assert reader.opened == 1


def test_zero_length_catalog_content_still_rejects_an_unavailable_locator():
    class UnavailableReader:
        def open(self, uri, *, offset=0, count=None):
            raise StoredObjectReadError("unconfigured rclone locator")

    empty = DeliveryRendition(
        kind="horizontal",
        storage_uri="rclone://atlas/KURUKIN_ATLAS/assets/mbe/source/asset--" + "a" * 64 + "/empty.mp4",
        thumbnail_uri=None,
        sha256="a" * 64,
        size_bytes=0,
        mime_type="video/mp4",
    )
    with client(delivery=asset(renditions={"horizontal": empty}), reader=UnavailableReader()) as http:
        response = http.get(f"/v1/assets/{ASSET_UID}/renditions/horizontal/content")
    assert (response.status_code, response.json()["code"]) == (503, "content_unavailable")
