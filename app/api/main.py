"""Atlas FastAPI entrypoint for frozen search and read-only asset delivery."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.dependencies import get_delivery_repository, get_search_repository
from app.contracts.asset_candidates_v1 import AssetCandidatesV1
from app.contracts.asset_error_v1 import AssetErrorCode, AssetErrorV1
from app.contracts.asset_search_v1 import AssetSearchV1
from app.delivery.reader import StoredObjectReadError, StoredObjectReader, get_stored_object_reader
from app.delivery.repository import DeliveryAsset, DeliveryRendition, DeliveryRepository
from app.search.errors import SearchPoolCapacityError
from app.search.service import SearchRepository, search_assets


RenditionRouteKind = Literal["horizontal", "vertical"]


class AssetApiError(Exception):
    def __init__(self, status_code: int, code: AssetErrorCode, message: str, *, retryable: bool = False) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


def _error(status_code: int, code: AssetErrorCode, message: str, *, retryable: bool = False) -> JSONResponse:
    body = AssetErrorV1(code=code, message=message, retryable=retryable)
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def _asset_and_rendition(repository: DeliveryRepository, asset_uid: UUID, kind: RenditionRouteKind) -> tuple[DeliveryAsset, DeliveryRendition]:
    asset = repository.get_asset(asset_uid)
    # Inactive catalog records are not deliverable and are intentionally not
    # distinguishable from an unknown public asset identity.
    if asset is None or asset.status != "active":
        raise AssetApiError(404, "asset_not_found", "Asset was not found.")
    rendition = asset.renditions.get(kind)
    if rendition is None:
        raise AssetApiError(404, "rendition_not_found", "Requested rendition was not found.")
    return asset, rendition


def _parse_single_range(value: str | None, total: int) -> tuple[int, int] | None:
    """Return inclusive bounds, or raise a sanitized 416 API error."""

    if value is None:
        return None
    invalid = AssetApiError(416, "invalid_request", "Requested byte range is not satisfiable.")
    if total < 0 or not value.startswith("bytes=") or "," in value:
        raise invalid
    spec = value[6:].strip()
    if spec.count("-") != 1:
        raise invalid
    start_raw, end_raw = spec.split("-", 1)
    try:
        if not start_raw:
            suffix = int(end_raw)
            if suffix <= 0:
                raise ValueError
            start = max(total - suffix, 0)
            end = total - 1
        else:
            start = int(start_raw)
            if start < 0 or start >= total:
                raise ValueError
            if end_raw:
                end = int(end_raw)
                if end < start:
                    raise ValueError
                end = min(end, total - 1)
            else:
                end = total - 1
    except ValueError as exc:
        raise invalid from exc
    if total == 0:
        raise invalid
    return start, end


def _content_headers(*, mime_type: str, size: int, sha256: str, partial: tuple[int, int] | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": mime_type,
        "Content-Length": str(size if partial is None else partial[1] - partial[0] + 1),
        "ETag": f'"{sha256}"',
        "Accept-Ranges": "bytes",
    }
    if partial is not None:
        headers["Content-Range"] = f"bytes {partial[0]}-{partial[1]}/{size}"
    return headers


def _close_stream(stream: Iterator[bytes]) -> None:
    """Close a reader iterator without replacing its original failure."""

    close = getattr(stream, "close", None)
    if close is not None:
        try:
            close()
        except Exception:
            # Storage/process cleanup details must never reach the API client.
            pass


class _PreflightedStream(Iterator[bytes]):
    """Return the already-read chunk, then own cleanup of the source iterator."""

    def __init__(self, stream: Iterator[bytes], first: bytes) -> None:
        self._stream = stream
        self._first = first
        self._first_pending = True
        self._closed = False

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        if self._first_pending:
            self._first_pending = False
            return self._first
        try:
            return next(self._stream)
        except BaseException:
            # This is necessarily post-header once the first chunk was yielded.
            # Preserve the original stream failure, but always release its
            # file descriptor or rclone subprocess.
            self.close()
            raise

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            _close_stream(self._stream)


def _open_stream(reader: StoredObjectReader, uri: str, *, offset: int = 0, count: int | None = None,
                 expected_size: int | None = None) -> Iterator[bytes]:
    """Open a stream and obtain one bounded chunk before HTTP headers commit.

    This lets open/startup failures become ``asset_error_v1`` while retaining
    streaming semantics: only one reader chunk is held, never the whole object.
    Closing the returned generator also closes the underlying rclone/local
    generator, which triggers its process/file cleanup.
    """

    stream = reader.open(uri, offset=offset, count=count)
    # A catalog-confirmed empty object has no body to prove. Still construct
    # the scheme-specific reader first: a malformed, unsupported, or
    # unconfigured locator must not turn into a successful empty response.
    if expected_size == 0:
        _close_stream(stream)
        return iter(())
    try:
        first = next(stream)
    except StopIteration:
        _close_stream(stream)
        if expected_size is not None and expected_size > 0:
            raise StoredObjectReadError("stored object ended before its cataloged length")
        return iter(())
    except Exception:
        _close_stream(stream)
        raise

    return _PreflightedStream(stream, first)


def create_app() -> FastAPI:
    app = FastAPI(title="Kurukin Atlas API", version="1.0.0")

    @app.exception_handler(AssetApiError)
    async def asset_api_error_handler(_: Request, exc: AssetApiError) -> JSONResponse:
        response = _error(exc.status_code, exc.code, exc.message, retryable=exc.retryable)
        if exc.status_code == 416:
            # RFC 9110 requires this form, including when the request itself is invalid.
            # The endpoint sets the actual total before raising this error.
            total = getattr(exc, "total", None)
            if total is not None:
                response.headers["Content-Range"] = f"bytes */{total}"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, __: RequestValidationError) -> JSONResponse:
        return _error(422, "invalid_request", "Request validation failed.")

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, __: Exception) -> JSONResponse:
        return _error(500, "internal_error", "An internal server error occurred.")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/v1/assets/search",
        response_model=AssetCandidatesV1,
        responses={422: {"model": AssetErrorV1}, 503: {"model": AssetErrorV1}, 500: {"model": AssetErrorV1}},
    )
    def asset_search(request: AssetSearchV1, repository: SearchRepository = Depends(get_search_repository)) -> AssetCandidatesV1:
        try:
            return search_assets(repository, request)
        except SearchPoolCapacityError as exc:
            raise AssetApiError(503, "search_capacity_exceeded", "Search capacity is temporarily exceeded.", retryable=True) from exc

    @app.get(
        "/v1/assets/{asset_uid}/renditions/{kind}/content",
        responses={422: {"model": AssetErrorV1}, 404: {"model": AssetErrorV1}, 416: {"model": AssetErrorV1}, 500: {"model": AssetErrorV1}, 503: {"model": AssetErrorV1}},
    )
    def content(asset_uid: UUID, kind: RenditionRouteKind, request: Request,
                repository: DeliveryRepository = Depends(get_delivery_repository),
                reader: StoredObjectReader = Depends(get_stored_object_reader)) -> StreamingResponse:
        _, rendition = _asset_and_rendition(repository, asset_uid, kind)
        if rendition.size_bytes is None or rendition.size_bytes < 0:
            raise AssetApiError(503, "content_unavailable", "Stored content is temporarily unavailable.", retryable=True)
        try:
            byte_range = _parse_single_range(request.headers.get("range"), rendition.size_bytes)
        except AssetApiError as exc:
            exc.total = rendition.size_bytes  # type: ignore[attr-defined]
            raise
        try:
            if byte_range is None:
                body = _open_stream(reader, rendition.storage_uri, expected_size=rendition.size_bytes)
                headers = _content_headers(mime_type=rendition.mime_type or "application/octet-stream", size=rendition.size_bytes, sha256=rendition.sha256)
                return StreamingResponse(body, status_code=200, headers=headers)
            start, end = byte_range
            body = _open_stream(
                reader,
                rendition.storage_uri,
                offset=start,
                count=end - start + 1,
                expected_size=end - start + 1,
            )
            headers = _content_headers(mime_type=rendition.mime_type or "application/octet-stream", size=rendition.size_bytes, sha256=rendition.sha256, partial=byte_range)
            return StreamingResponse(body, status_code=206, headers=headers)
        except StoredObjectReadError as exc:
            raise AssetApiError(503, "content_unavailable", "Stored content is temporarily unavailable.", retryable=True) from exc

    @app.get(
        "/v1/assets/{asset_uid}/renditions/{kind}/thumbnail",
        responses={422: {"model": AssetErrorV1}, 404: {"model": AssetErrorV1}, 500: {"model": AssetErrorV1}, 503: {"model": AssetErrorV1}},
    )
    def thumbnail(asset_uid: UUID, kind: RenditionRouteKind,
                  repository: DeliveryRepository = Depends(get_delivery_repository),
                  reader: StoredObjectReader = Depends(get_stored_object_reader)) -> StreamingResponse:
        _, rendition = _asset_and_rendition(repository, asset_uid, kind)
        if rendition.thumbnail_uri is None:
            raise AssetApiError(404, "thumbnail_not_available", "Thumbnail is not available.")
        if rendition.thumbnail_size_bytes is not None and rendition.thumbnail_size_bytes < 0:
            raise AssetApiError(503, "content_unavailable", "Stored content is temporarily unavailable.", retryable=True)
        try:
            body = _open_stream(reader, rendition.thumbnail_uri, expected_size=rendition.thumbnail_size_bytes)
            headers = {"Content-Type": "image/jpeg"}
            if rendition.thumbnail_size_bytes is not None:
                headers["Content-Length"] = str(rendition.thumbnail_size_bytes)
            if rendition.thumbnail_sha256:
                headers["ETag"] = f'"{rendition.thumbnail_sha256}"'
            return StreamingResponse(body, status_code=200, headers=headers)
        except StoredObjectReadError as exc:
            raise AssetApiError(503, "content_unavailable", "Stored content is temporarily unavailable.", retryable=True) from exc

    return app


app = create_app()
