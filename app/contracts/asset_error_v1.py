"""Stable, sanitized HTTP error contract for Atlas asset APIs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


AssetErrorCode = Literal[
    "invalid_request",
    "search_capacity_exceeded",
    "asset_not_found",
    "rendition_not_found",
    "thumbnail_not_available",
    "content_unavailable",
    "internal_error",
]


class AssetErrorV1(BaseModel):
    """Client-safe errors; operational details intentionally stay server-side."""

    model_config = ConfigDict(extra="forbid", title="asset_error_v1")

    schema_version: Literal["asset_error_v1"] = "asset_error_v1"
    code: AssetErrorCode
    message: str
    retryable: bool
    details: dict[str, Any] | None = None
